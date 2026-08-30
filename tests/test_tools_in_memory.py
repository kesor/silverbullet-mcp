"""Layer 1 tests for ``server.py``: in-memory ``Client(mcp)`` with a
mocked SB transport.

We substitute ``httpx.MockTransport`` for a real SilverBullet so the
test suite never needs a running SB. The full HTTP integration matrix
(bridge asgi on a real socket + real auth + discovery doc) lives in
``tests/test_http_auth`` (Layer 2), built on top of this module.

Coverage:

- All three tools (``read_page``, ``write_page``, ``list_pages``) on
  the 200 happy path; ``write_page`` returns the ETag, ``list_pages``
  returns the file metas; ``read_page`` and the resource template both
  surface the markdown body.
- ``write_page`` carries the ``if_match`` straight through to
  ``sb_client`` (T3 covers the wire envelope; this test guards the
  MCP-tool-to-SB-client argument path).
- ``list_pages`` filters by prefix client-side.
- Each SB exception maps to ``is_error=True`` with the design doc's
  exact ToolError message: 404 → "page not found: <name>"; 412 →
  "precondition failed; check if_match/if_none_match"; 413 →
  "body too large: limit is 4 MiB"; 5xx → "silverbullet error: <status>";
  timeout → "silverbullet request timed out".
- The resource template returns the same body for the happy path and
  surfaces ``ToolError`` for a missing page (v1 keeps one error shape
  for both surfaces; T4 carry-forward note in the map).
"""

from __future__ import annotations

import httpx2 as httpx
import pytest
from mcp.client import Client

from mcp_silverbullet.sb_client import SBClient
from mcp_silverbullet.server import build_mcp


TOKEN = "test-secret-do-not-use-in-prod"
SB_URL = "http://sb.test"
RESOURCE_URL = "http://bridge.test/mcp"


def _build(handler) -> MCPServer:
    """Build an MCP server whose underlying SB transport is ``handler``.

    ``handler`` is an ``httpx.MockTransport`` callable — it receives
    every request the bridge makes to SB and returns a synthetic
    ``httpx.Response``. The same trick ``tests/test_sb_client.py`` uses
    to test the outbound half; here it lets the in-memory MCP client
    exercise the full tool pipeline without a real SilverBullet.
    """
    transport = httpx.MockTransport(handler)
    sb = SBClient.__new__(SBClient)
    sb._client = httpx.AsyncClient(
        base_url=SB_URL,
        headers={"Authorization": f"Bearer {TOKEN}"},
        transport=transport,
    )
    return build_mcp(sb, token=TOKEN, resource_url=RESOURCE_URL)


def _text(result) -> str:
    """Concatenate the text content of a tool call result."""
    return "".join(
        block.text for block in result.content if getattr(block, "type", None) == "text"
    )


# --- read_page ---------------------------------------------------------


@pytest.mark.asyncio
async def test_read_page_returns_body_on_200() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/.fs/index"
        return httpx.Response(200, text="# hello")

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("read_page", {"name": "index"})

    assert result.is_error is False
    assert _text(result) == "# hello"


@pytest.mark.asyncio
async def test_read_page_404_returns_tool_error_with_design_doc_wording() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="page not found")

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("read_page", {"name": "missing"})

    assert result.is_error is True
    # The SDK prefixes the ToolError message with "Error executing
    # tool <name>: "; the design-doc wording "page not found: <name>"
    # is what our handler raises, and the SDK adds the prefix on the
    # wire. Both shapes are stable; assert on the full wire text so a
    # future SDK change to the prefix is caught loudly.
    assert _text(result) == "Error executing tool read_page: page not found: missing"


@pytest.mark.asyncio
async def test_read_page_5xx_returns_tool_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="upstream gone")

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("read_page", {"name": "anything"})

    assert result.is_error is True
    assert _text(result) == "Error executing tool read_page: silverbullet error: 503"


# --- write_page --------------------------------------------------------


@pytest.mark.asyncio
async def test_write_page_returns_etag_on_200() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"ETag": '"abc123"'})

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "write_page",
            {"name": "index", "content": "# new body"},
        )

    assert result.is_error is False
    assert _text(result) == '"abc123"'


@pytest.mark.asyncio
async def test_write_page_returns_null_when_etag_header_missing() -> None:
    """A 200 with no ETag header (older SB / proxy-stripped) → ``None``.

    Guard against a future refactor that drops the ``None`` fallback
    and instead returns ``""`` (a different and confusing wire shape).
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200)

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "write_page",
            {"name": "index", "content": "# new body"},
        )

    assert result.is_error is False
    # ``str | None`` returns are wrapped in ``{"result": ...}`` by the
    # SDK; missing ETag becomes JSON ``null``.
    assert result.structured_content == {"result": None}


@pytest.mark.asyncio
async def test_write_page_forwards_if_match() -> None:
    """The MCP tool passes ``if_match`` straight to ``sb_client.write_page``.

    T3 covers the wire envelope (X-Source / X-Permission / Content-Type /
    If-Match on the actual HTTP request); this test guards the MCP
    argument path so a future refactor doesn't silently drop the
    parameter or coerce it to the empty string.
    """
    seen_match: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_match.append(request.headers.get("If-Match", ""))
        return httpx.Response(200, headers={"ETag": '"v2"'})

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "write_page",
            {"name": "index", "content": "body", "if_match": '"v1"'},
        )

    assert result.is_error is False
    assert seen_match == ['"v1"']


@pytest.mark.asyncio
async def test_write_page_404_returns_tool_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="page not found")

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "write_page", {"name": "missing", "content": "body"}
        )

    assert result.is_error is True
    assert _text(result) == "Error executing tool write_page: page not found: missing"


@pytest.mark.asyncio
async def test_write_page_412_returns_tool_error_with_design_doc_wording() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(412, text="precondition failed")

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "write_page",
            {"name": "index", "content": "body", "if_match": "*"},
        )

    assert result.is_error is True
    assert _text(result) == "Error executing tool write_page: precondition failed; check if_match/if_none_match"


@pytest.mark.asyncio
async def test_write_page_413_returns_tool_error_with_4_mib_wording() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(413, text="body too large")

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "write_page", {"name": "index", "content": "x" * 1024}
        )

    assert result.is_error is True
    assert _text(result) == "Error executing tool write_page: body too large: limit is 4 MiB"


@pytest.mark.asyncio
async def test_write_page_5xx_returns_tool_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="bad gateway")

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "write_page", {"name": "index", "content": "body"}
        )

    assert result.is_error is True
    assert _text(result) == "Error executing tool write_page: silverbullet error: 502"


# --- delete_page -------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_page_returns_etag_on_200() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        assert request.url.path == "/.fs/index"
        return httpx.Response(200, headers={"ETag": '"abc123"'})

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("delete_page", {"name": "index"})

    assert result.is_error is False
    assert _text(result) == '"abc123"'


@pytest.mark.asyncio
async def test_delete_page_returns_null_when_etag_header_missing() -> None:
    """A 200 with no ETag header → ``None``.

    Mirror of :func:`test_write_page_returns_null_when_etag_header_missing`:
    the wire shape is ``{"result": null}`` and any future refactor
    that returns ``""`` would be a confusing type drift.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200)

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("delete_page", {"name": "index"})

    assert result.is_error is False
    assert result.structured_content == {"result": None}


@pytest.mark.asyncio
async def test_delete_page_forwards_if_match_star() -> None:
    """``if_match="*"`` requires the page to exist; the bridge forwards ``If-Match: *`` verbatim.

    Mirrors :func:`test_write_page_forwards_if_match` for the
    delete path so a future refactor doesn't drop the parameter on
    one tool but not the other.
    """
    seen_match: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_match.append(request.headers.get("If-Match", ""))
        return httpx.Response(200, headers={"ETag": '"v2"'})

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "delete_page",
            {"name": "index", "if_match": "*"},
        )

    assert result.is_error is False
    assert seen_match == ["*"]


@pytest.mark.asyncio
async def test_delete_page_404_returns_tool_error_with_design_doc_wording() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="page not found")

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("delete_page", {"name": "missing"})

    assert result.is_error is True
    assert _text(result) == "Error executing tool delete_page: page not found: missing"


@pytest.mark.asyncio
async def test_delete_page_412_with_if_match_star_returns_tool_error() -> None:
    """``if_match=\"*\"`` on a missing page returns 412 (not 404) at the SB layer.

    SB's semantics: ``\"*\"`` is *must exist*; a missing page is a
    precondition failure, not a not-found. The bridge surfaces the
    unified 412 ToolError wording so callers don't need to distinguish
    “missing” from “stale etag” — they just got refused; they can
    ``read_page`` to figure out which.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(412, text="precondition failed")

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "delete_page", {"name": "missing", "if_match": "*"}
        )

    assert result.is_error is True
    assert _text(result) == "Error executing tool delete_page: precondition failed; check if_match/if_none_match"


@pytest.mark.asyncio
async def test_delete_page_412_with_stale_if_match_returns_tool_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(412, text="precondition failed")

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "delete_page",
            {"name": "index", "if_match": '"stale"'},
        )

    assert result.is_error is True
    assert _text(result) == "Error executing tool delete_page: precondition failed; check if_match/if_none_match"


@pytest.mark.asyncio
async def test_delete_page_5xx_returns_tool_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="bad gateway")

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("delete_page", {"name": "anything"})

    assert result.is_error is True
    assert _text(result) == "Error executing tool delete_page: silverbullet error: 502"


# --- list_pages --------------------------------------------------------


@pytest.mark.asyncio
async def test_list_pages_returns_file_metas_on_200() -> None:
    payload = [
        {"name": "index", "etag": '"a"', "size": 12},
        {"name": "page-2", "etag": None, "size": 7},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/.fs"
        return httpx.Response(200, content=__import__("json").dumps(payload).encode())

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("list_pages", {})

    assert result.is_error is False
    # The tool returns ``list[dict[str, str | None]]``; the SDK
    # serialises that as structured content (``structured_content`` in
    # Python, ``structuredContent`` on the wire). Asserting on the
    # structured payload avoids string-encoding fragility.
    assert result.structured_content == {
        "result": [
            {"name": "index", "etag": '"a"'},
            {"name": "page-2", "etag": None},
        ]
    }


@pytest.mark.asyncio
async def test_list_pages_filters_by_prefix() -> None:
    payload = [
        {"name": "index", "etag": None},
        {"name": "journal/2026-01-01", "etag": None},
        {"name": "journal/2026-01-02", "etag": None},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=__import__("json").dumps(payload).encode())

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("list_pages", {"prefix": "journal/"})

    assert result.is_error is False
    assert result.structured_content == {
        "result": [
            {"name": "journal/2026-01-01", "etag": None},
            {"name": "journal/2026-01-02", "etag": None},
        ]
    }


@pytest.mark.asyncio
async def test_list_pages_5xx_returns_tool_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("list_pages", {})

    assert result.is_error is True
    assert _text(result) == "Error executing tool list_pages: silverbullet error: 500"


# --- resource template -------------------------------------------------


@pytest.mark.asyncio
async def test_resource_template_returns_markdown_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/.fs/index"
        return httpx.Response(200, text="# page body")

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.read_resource("silverbullet://page/index")

    assert len(result.contents) == 1
    content = result.contents[0]
    assert getattr(content, "mime_type", None) == "text/markdown"
    assert content.text == "# page body"


@pytest.mark.asyncio
async def test_resource_template_404_raises_resource_not_found_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="page not found")

    from mcp.shared.exceptions import MCPError

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        with pytest.raises(MCPError) as exc_info:
            await client.read_resource("silverbullet://page/missing")

    # The SDK maps ``ResourceNotFoundError`` to ``-32602 invalid
    # params`` per SEP-2164; the message text is what Grok surfaces.
    assert "page not found: missing" in str(exc_info.value)


# --- timeout -----------------------------------------------------------


@pytest.mark.asyncio
async def test_read_page_timeout_returns_tool_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("simulated")

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("read_page", {"name": "anything"})

    assert result.is_error is True
    assert _text(result) == "Error executing tool read_page: silverbullet request timed out"

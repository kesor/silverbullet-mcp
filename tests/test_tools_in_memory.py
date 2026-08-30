"""Layer 1 tests for ``server.py``: in-memory ``Client(mcp)`` with a
mocked SB transport.

We substitute ``httpx.MockTransport`` for a real SilverBullet so the
test suite never needs a running SB. The full HTTP integration matrix
(bridge asgi on a real socket + real auth + discovery doc) lives in
``tests/test_http_auth`` (Layer 2), built on top of this module.

Coverage:

- All seven ``/.fs``-backed tools (``read_page``, ``write_page``,
  ``delete_page``, ``append_to_page``, ``patch_page_lines``,
  ``patch_page_replace``, ``list_pages``) on the 200 happy path;
  ``write_page`` / ``delete_page`` / ``append_to_page`` /
  ``patch_page_lines`` / ``patch_page_replace`` return the ETag,
  ``list_pages`` returns the file metas, ``read_page`` and the
  resource template both surface the markdown body.
  ``append_to_page`` (T19), ``patch_page_lines`` (T20), and
  ``patch_page_replace`` (T21) are the read-modify-write tools.
- ``write_page`` carries the ``if_match`` straight through to
  ``sb_client`` (T3 covers the wire envelope; this test guards the
  MCP-tool-to-SB-client argument path).
- ``list_pages`` filters by prefix client-side.
- Each SB exception maps to ``is_error=True`` with the design doc's
  exact ToolError message: 404 → "page not found: <name>"; 412 →
  "precondition failed; check if_match/if_none_match"; 413 →
  "body too large: limit is 4 MiB"; 5xx → "silverbullet error: <status>";
  timeout → "silverbullet request timed out". The seven tools share
  the translation through :func:`server._translate_sb_errors`.
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


# --- append_to_page ----------------------------------------------------


@pytest.mark.asyncio
async def test_append_to_page_returns_etag_on_200() -> None:
    """Happy path: existing body + new text → read-modify-write round trip.

    Captures the GET (read) and PUT (write) the tool issues so we
    can assert: (a) the read happened first, (b) the write carries
    the combined body, (c) the tool returns the write's ETag, not
    the read's.
    """
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            calls.append(("GET", request.url.path))
            return httpx.Response(200, text="hello\n")
        # PUT
        calls.append(("PUT", request.url.path))
        assert request.content == b"hello\nworld"
        return httpx.Response(200, headers={"ETag": '"v2"'})

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "append_to_page",
            {"name": "index", "text": "world"},
        )

    assert result.is_error is False
    assert _text(result) == '"v2"'
    # Read first, then write — locks the read-modify-write ordering.
    assert calls == [
        ("GET", "/.fs/index"),
        ("PUT", "/.fs/index"),
    ]


@pytest.mark.asyncio
async def test_append_to_page_separator_inserted_when_body_lacks_newline() -> None:
    """Body ``"goodbye"`` + ``"hello"`` → ``"goodbye\\nhello"``.

    Locks the separator rule: exactly one newline inserted between
    the two halves when the body doesn't already end in ``\\n``.
    """
    seen_body: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text="goodbye")
        seen_body.append(request.content)
        return httpx.Response(200, headers={"ETag": '"v2"'})

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        await client.call_tool(
            "append_to_page",
            {"name": "index", "text": "hello"},
        )

    assert seen_body == [b"goodbye\nhello"]


@pytest.mark.asyncio
async def test_append_to_page_no_extra_separator_when_body_ends_in_newline() -> None:
    """Body ``"hello\\n"`` + ``"world"`` → ``"hello\\nworld"``.

    No double-separator: the body already ends in ``\\n``, so the
    tool only concatenates.
    """
    seen_body: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text="hello\n")
        seen_body.append(request.content)
        return httpx.Response(200, headers={"ETag": '"v2"'})

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        await client.call_tool(
            "append_to_page",
            {"name": "index", "text": "world"},
        )

    assert seen_body == [b"hello\nworld"]


@pytest.mark.asyncio
async def test_append_to_page_no_extra_separator_for_multiple_trailing_newlines() -> None:
    """Body ``"hello\\n\\n"`` + ``"world"`` → ``"hello\\n\\nworld"``.

    The rule is ``endswith("\\n")`` (one newline), not
    ``endswith("\\n\\n")`` (two). The trailing-newline case is
    idempotent for callers that want exactly one separator.
    """
    seen_body: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text="hello\n\n")
        seen_body.append(request.content)
        return httpx.Response(200, headers={"ETag": '"v2"'})

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        await client.call_tool(
            "append_to_page",
            {"name": "index", "text": "world"},
        )

    assert seen_body == [b"hello\n\nworld"]


@pytest.mark.asyncio
async def test_append_to_page_works_on_empty_body() -> None:
    """Body ``""`` + ``"hello"`` → ``"hello"``.

    The no-body case: no separator. An empty page is a valid
    append target — the first append just becomes the body.
    """
    seen_body: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text="")
        seen_body.append(request.content)
        return httpx.Response(200, headers={"ETag": '"v1"'})

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "append_to_page",
            {"name": "index", "text": "hello"},
        )

    assert result.is_error is False
    assert seen_body == [b"hello"]


@pytest.mark.asyncio
async def test_append_to_page_text_with_leading_newline_does_not_double_separator() -> None:
    """Body ``"hello"`` + ``"\\nworld"`` → ``"hello\\n\\nworld"``.

    The tool prepends one ``\\n`` when the body doesn't end in one;
    if the caller also supplies a leading ``\\n`` they get exactly
    one separator between the two halves plus the caller's own
    newline.
    """
    seen_body: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text="hello")
        seen_body.append(request.content)
        return httpx.Response(200, headers={"ETag": '"v2"'})

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        await client.call_tool(
            "append_to_page",
            {"name": "index", "text": "\nworld"},
        )

    assert seen_body == [b"hello\n\nworld"]


@pytest.mark.asyncio
async def test_append_to_page_empty_text_returns_tool_error() -> None:
    """``text=""`` is rejected upfront — no read-modify-write round trip.

    An empty append is almost certainly a caller bug. Catching it
    here saves the GET + PUT that would have been a no-op.
    """
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        return httpx.Response(200, text="hello")

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "append_to_page",
            {"name": "index", "text": ""},
        )

    assert result.is_error is True
    assert _text(result) == "Error executing tool append_to_page: text must not be empty"
    # No GET, no PUT — the rejection is upfront.
    assert calls == []


@pytest.mark.asyncio
async def test_append_to_page_forwards_if_match_to_write() -> None:
    """``if_match`` is forwarded to the PUT, not the GET.

    The tool's contract: ``if_match`` guards the *write*, not the
    read. A caller who wants "append if no one else has written
    since I last read" passes the etag they got from their last
    ``read_page``; the bridge threads it straight into
    ``If-Match`` on the PUT. The GET carries no precondition.
    """
    seen_if_match: list[str] = []
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        seen_if_match.append(request.headers.get("If-Match", ""))
        if request.method == "GET":
            return httpx.Response(200, text="hello")
        return httpx.Response(200, headers={"ETag": '"v2"'})

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        await client.call_tool(
            "append_to_page",
            {"name": "index", "text": "world", "if_match": '"v1"'},
        )

    # Read first, then write; If-Match only on the write.
    assert calls == ["GET", "PUT"]
    assert seen_if_match == ["", '"v1"']


@pytest.mark.asyncio
async def test_append_to_page_forwards_if_match_star() -> None:
    """``if_match=\"*\"`` requires the page to exist (the write layer checks).

    Coherent with ``write_page``'s ``if_match=\"*\"`` semantics. The
    bridge does not treat this as a *create* — that's
    ``write_page(name, content, if_match=\"*\")``. The read
    happens unconditionally and surfaces a 404 if the page is
    missing (no second 412 from the write path because the page
    isn't there yet).
    """
    seen_if_match: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text="hello")
        seen_if_match.append(request.headers.get("If-Match", ""))
        return httpx.Response(200, headers={"ETag": '"v2"'})

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        await client.call_tool(
            "append_to_page",
            {"name": "index", "text": "world", "if_match": "*"},
        )

    assert seen_if_match == ["*"]


@pytest.mark.asyncio
async def test_append_to_page_404_returns_tool_error() -> None:
    """Read raises 404 → ``ToolError("page not found: {name}")``."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="page not found")

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "append_to_page", {"name": "missing", "text": "world"}
        )

    assert result.is_error is True
    assert _text(result) == "Error executing tool append_to_page: page not found: missing"


@pytest.mark.asyncio
async def test_append_to_page_412_returns_tool_error_with_design_doc_wording() -> None:
    """Stale ``if_match`` on the PUT surfaces the 412 wording.

    The read succeeds; the write fails 412. The bridge maps to the
    unified 412 ``ToolError`` wording so callers don't need to
    distinguish "stale etag" from "if_match='*' on a missing page"
    — they just got refused; they can ``read_page`` to figure out
    which.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text="hello")
        return httpx.Response(412, text="precondition failed")

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "append_to_page",
            {"name": "index", "text": "world", "if_match": '"stale"'},
        )

    assert result.is_error is True
    assert _text(result) == "Error executing tool append_to_page: precondition failed; check if_match/if_none_match"


@pytest.mark.asyncio
async def test_append_to_page_413_returns_tool_error_with_4_mib_wording() -> None:
    """Combined body > 4 MiB → ``ToolError("body too large: limit is 4 MiB")``."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text="hello")
        return httpx.Response(413, text="body too large")

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "append_to_page", {"name": "index", "text": "world"}
        )

    assert result.is_error is True
    assert _text(result) == "Error executing tool append_to_page: body too large: limit is 4 MiB"


@pytest.mark.asyncio
async def test_append_to_page_5xx_returns_tool_error() -> None:
    """SB 5xx surfaces the ``ToolError("silverbullet error: {status}")`` wording.

    Either the read or the write can 5xx — the unified wording
    applies in both cases because both paths run through
    :func:`_translate_sb_errors`.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text="hello")
        return httpx.Response(502, text="bad gateway")

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "append_to_page", {"name": "index", "text": "world"}
        )

    assert result.is_error is True
    assert _text(result) == "Error executing tool append_to_page: silverbullet error: 502"


@pytest.mark.asyncio
async def test_append_to_page_returns_null_when_etag_header_missing() -> None:
    """A successful write with no ``ETag`` header → ``None``.

    Mirror of the same shape on :func:`test_write_page_returns_null_when_etag_header_missing`
    and ``delete_page``. The wire payload is ``{"result": null}``
    and any future refactor that returned ``""`` would be a
    confusing type drift for callers chaining edits.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text="hello\n")
        return httpx.Response(200)

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "append_to_page", {"name": "index", "text": "world"}
        )

    assert result.is_error is False
    assert result.structured_content == {"result": None}


# --- patch_page_lines --------------------------------------------------


@pytest.mark.asyncio
async def test_patch_page_lines_returns_etag_on_200() -> None:
    """Happy path: replace middle range, get the new ETag back.

    Locks the read-modify-write shape: the tool reads first, then
    writes the patched body, and returns the write's ETag (not the
    read's). The patched body is asserted on directly so a future
    refactor that drops or duplicates lines fails loudly.
    """
    seen_writes: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text="a\nb\nc\nd")
        seen_writes.append(request.content)
        return httpx.Response(200, headers={"ETag": '"v2"'})

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "patch_page_lines",
            {
                "name": "index",
                "start_line": 2,
                "end_line": 3,
                "new_content": "B\nC",
            },
        )

    assert result.is_error is False
    assert _text(result) == '"v2"'
    # a\nB\nC\nd — lines 2-3 (b, c) replaced by B\nC; the surrounding
    # lines (a, d) are untouched.
    assert seen_writes == [b"a\nB\nC\nd"]


@pytest.mark.asyncio
async def test_patch_page_lines_replaces_first_n_lines() -> None:
    """``start_line=1, end_line=N`` replaces the first N lines."""
    seen_writes: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text="a\nb\nc")
        seen_writes.append(request.content)
        return httpx.Response(200, headers={"ETag": '"v2"'})

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        await client.call_tool(
            "patch_page_lines",
            {
                "name": "index",
                "start_line": 1,
                "end_line": 2,
                "new_content": "A\nB",
            },
        )

    assert seen_writes == [b"A\nB\nc"]


@pytest.mark.asyncio
async def test_patch_page_lines_replaces_last_n_lines() -> None:
    """``start_line=N-k, end_line=N`` replaces the last k lines."""
    seen_writes: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text="a\nb\nc\nd")
        seen_writes.append(request.content)
        return httpx.Response(200, headers={"ETag": '"v2"'})

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        await client.call_tool(
            "patch_page_lines",
            {
                "name": "index",
                "start_line": 3,
                "end_line": 4,
                "new_content": "C\nD",
            },
        )

    assert seen_writes == [b"a\nb\nC\nD"]


@pytest.mark.asyncio
async def test_patch_page_lines_replaces_entire_body() -> None:
    """``start_line=1, end_line=line_count, new_content=\"x\"`` replaces everything.

    Edge case: the result is just ``\"x\"``, no surrounding lines.
    """
    seen_writes: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text="a\nb\nc")
        seen_writes.append(request.content)
        return httpx.Response(200, headers={"ETag": '"v2"'})

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        await client.call_tool(
            "patch_page_lines",
            {
                "name": "index",
                "start_line": 1,
                "end_line": 3,
                "new_content": "x",
            },
        )

    assert seen_writes == [b"x"]


@pytest.mark.asyncio
async def test_patch_page_lines_empty_new_content_deletes_range() -> None:
    """``new_content=\"\"`` deletes the range without adding a replacement.

    Locks the standing-preference rule from the v1.1 map: empty
    ``new_content`` deletes the range, no separate ``delete_lines``
    tool.
    """
    seen_writes: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text="a\nb\nc\nd")
        seen_writes.append(request.content)
        return httpx.Response(200, headers={"ETag": '"v2"'})

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        await client.call_tool(
            "patch_page_lines",
            {
                "name": "index",
                "start_line": 2,
                "end_line": 3,
                "new_content": "",
            },
        )

    # ``a\\nd`` — the middle two lines (b, c) were deleted.
    assert seen_writes == [b"a\nd"]


@pytest.mark.asyncio
async def test_patch_page_lines_preserves_trailing_newline() -> None:
    """Body ``\"a\\nb\\n\"`` patched stays ``\"a\\nX\\n\"`` (trailing ``\\n`` preserved).

    Locks the editor-style trailing-newline preservation: the
    read-modify-write round trip doesn't strip the file's final
    newline when it was there to begin with.
    """
    seen_writes: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text="a\nb\n")
        seen_writes.append(request.content)
        return httpx.Response(200, headers={"ETag": '"v2"'})

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        await client.call_tool(
            "patch_page_lines",
            {
                "name": "index",
                "start_line": 2,
                "end_line": 2,
                "new_content": "X",
            },
        )

    assert seen_writes == [b"a\nX\n"]


@pytest.mark.asyncio
async def test_patch_page_lines_no_trailing_newline_added_if_body_lacks_one() -> None:
    """Body ``\"a\\nb\"`` patched stays ``\"a\\nX\"`` (no trailing ``\\n`` added).

    Counterpart to the previous test: if the body had no trailing
    newline, the patched result has no trailing newline either. The
    tool doesn't *add* a newline.
    """
    seen_writes: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text="a\nb")
        seen_writes.append(request.content)
        return httpx.Response(200, headers={"ETag": '"v2"'})

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        await client.call_tool(
            "patch_page_lines",
            {
                "name": "index",
                "start_line": 2,
                "end_line": 2,
                "new_content": "X",
            },
        )

    assert seen_writes == [b"a\nX"]


@pytest.mark.asyncio
async def test_patch_page_lines_new_content_with_trailing_newline_does_not_double_up() -> None:
    """``new_content=\"X\\n\"`` against body ``\"a\\nb\\n\"`` → ``\"a\\nX\\n\"``.

    The replacement's trailing newline is dropped during the split
    (same as the body's trailing-newline drop) and re-attached at
    the end iff the body had one — so the result has exactly one
    ``\\n`` after ``X``, not two. The replacement's trailing
    newline effectively becomes the line terminator of the last
    replacement line.
    """
    seen_writes: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text="a\nb\n")
        seen_writes.append(request.content)
        return httpx.Response(200, headers={"ETag": '"v2"'})

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        await client.call_tool(
            "patch_page_lines",
            {
                "name": "index",
                "start_line": 2,
                "end_line": 2,
                "new_content": "X\n",
            },
        )

    assert seen_writes == [b"a\nX\n"]


@pytest.mark.asyncio
async def test_patch_page_lines_start_line_zero_returns_tool_error() -> None:
    """``start_line=0`` is invalid (lines are 1-indexed) → ``ToolError`` upfront.

    Pre-read validation: the read-modify-write round trip is
    skipped because the input is clearly bad. The error wording
    doesn't carry the page's line count (it isn't known yet).
    """
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        return httpx.Response(200, text="a\nb")

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "patch_page_lines",
            {
                "name": "index",
                "start_line": 0,
                "end_line": 1,
                "new_content": "X",
            },
        )

    assert result.is_error is True
    assert _text(result) == "Error executing tool patch_page_lines: start_line must be >= 1, got 0"
    assert calls == []


@pytest.mark.asyncio
async def test_patch_page_lines_negative_start_line_returns_tool_error() -> None:
    """``start_line=-1`` is invalid → ``ToolError`` upfront, no GET/PUT."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        return httpx.Response(200, text="a\nb")

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "patch_page_lines",
            {
                "name": "index",
                "start_line": -1,
                "end_line": 1,
                "new_content": "X",
            },
        )

    assert result.is_error is True
    assert _text(result) == "Error executing tool patch_page_lines: start_line must be >= 1, got -1"
    assert calls == []


@pytest.mark.asyncio
async def test_patch_page_lines_end_line_less_than_start_line_returns_tool_error() -> None:
    """``end_line < start_line`` (inverted range) → ``ToolError`` upfront.

    No read needed: an inverted range is always wrong, regardless
    of the page's content. The error carries both endpoints so a
    caller can see exactly which direction they inverted.
    """
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        return httpx.Response(200, text="a\nb\nc")

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "patch_page_lines",
            {
                "name": "index",
                "start_line": 3,
                "end_line": 2,
                "new_content": "X",
            },
        )

    assert result.is_error is True
    assert _text(result) == "Error executing tool patch_page_lines: end_line (2) must be >= start_line (3)"
    assert calls == []


@pytest.mark.asyncio
async def test_patch_page_lines_end_line_past_last_line_returns_tool_error() -> None:
    """``end_line > line_count`` → ``ToolError`` with the page's line count.

    Post-read validation: we needed the read to know the page has
    only N lines. The wording matches the v1.1 map's recommended
    ``\"line range {start}..{end} out of bounds for page with {N} lines\"``.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text="a\nb\nc")
        # Should not reach: the write is gated by the bounds check.
        return httpx.Response(200, headers={"ETag": '"v2"'})

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "patch_page_lines",
            {
                "name": "index",
                "start_line": 2,
                "end_line": 99,
                "new_content": "X",
            },
        )

    assert result.is_error is True
    assert _text(result) == "Error executing tool patch_page_lines: line range 2..99 out of bounds for page with 3 lines"


@pytest.mark.asyncio
async def test_patch_page_lines_patches_into_an_empty_page() -> None:
    """Body ``\"\"`` + ``patch_page_lines(name, 1, 1, \"x\")`` → ``\"x\"``.

    Empty pages are a valid patch target: the tool splits ``\"\"``
    into ``[]`` (zero lines) but raises out-of-bounds for any
    ``end_line > 0``. Lock that the *only* legal patch on an empty
    page is the no-op range — anything else errors with the empty
    page's line count.
    """
    seen_writes: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text="")
        seen_writes.append(request.content)
        return httpx.Response(200, headers={"ETag": '"v1"'})

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "patch_page_lines",
            {
                "name": "index",
                "start_line": 1,
                "end_line": 1,
                "new_content": "x",
            },
        )

    # ``end_line=1`` against ``[]`` is ``end_line > line_count`` —
    # out of bounds. Empty pages can only be patched by replacing
    # the whole thing via ``write_page``.
    assert result.is_error is True
    assert _text(result) == "Error executing tool patch_page_lines: line range 1..1 out of bounds for page with 0 lines"
    assert seen_writes == []


@pytest.mark.asyncio
async def test_patch_page_lines_replaces_only_line_in_single_line_page() -> None:
    """Body ``\"a\"`` + ``patch_page_lines(name, 1, 1, \"X\")`` → ``\"X\"``."""
    seen_writes: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text="a")
        seen_writes.append(request.content)
        return httpx.Response(200, headers={"ETag": '"v2"'})

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        await client.call_tool(
            "patch_page_lines",
            {
                "name": "index",
                "start_line": 1,
                "end_line": 1,
                "new_content": "X",
            },
        )

    assert seen_writes == [b"X"]


@pytest.mark.asyncio
async def test_patch_page_lines_handles_universal_newlines_in_body() -> None:
    """The split is on ``\\n`` only, not ``splitlines``.

    A body containing ``\\r\\n`` line endings (which SB doesn't emit
    but a stray editor could have left behind) gets one CR character
    at the end of each line — the patch treats the CR as part of the
    line content. The behavior is locked down so a future refactor
    that calls ``splitlines()`` (and silently strips ``\\r``) gets
    caught loudly.
    """
    seen_writes: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text="a\r\nb\r\nc")
        seen_writes.append(request.content)
        return httpx.Response(200, headers={"ETag": '"v2"'})

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        await client.call_tool(
            "patch_page_lines",
            {
                "name": "index",
                "start_line": 2,
                "end_line": 2,
                "new_content": "B",
            },
        )

    # Lines are ``"a\r"``, ``"b\r"``, ``"c"`` (split on ``\n``, CR
    # preserved as line content). The join inserts ``\n`` between
    # elements, so replacing ``"b\r"`` with ``"B"`` gives
    # ``"a\r\nB\nc"``. ``str.splitlines()`` would have stripped the
    # ``\r`` and produced ``"a\nB\nc"`` — a quieter divergence.
    assert seen_writes == [b"a\r\nB\nc"]


@pytest.mark.asyncio
async def test_patch_page_lines_forwards_if_match_to_write() -> None:
    """``if_match`` is forwarded to the PUT, not the GET.

    Same contract as ``append_to_page`` (T19): the precondition
    guards the *write*, not the read. A stale etag from the
    caller's last ``read_page`` is the typical use case.
    """
    seen_if_match: list[str] = []
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        seen_if_match.append(request.headers.get("If-Match", ""))
        if request.method == "GET":
            return httpx.Response(200, text="a\nb")
        return httpx.Response(200, headers={"ETag": '"v2"'})

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        await client.call_tool(
            "patch_page_lines",
            {
                "name": "index",
                "start_line": 1,
                "end_line": 1,
                "new_content": "A",
                "if_match": '"v1"',
            },
        )

    assert calls == ["GET", "PUT"]
    assert seen_if_match == ["", '"v1"']


@pytest.mark.asyncio
async def test_patch_page_lines_stale_if_match_returns_412_tool_error() -> None:
    """Stale ``if_match`` on the PUT → ``ToolError(\"precondition failed; …\")``."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text="a\nb")
        return httpx.Response(412, text="precondition failed")

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "patch_page_lines",
            {
                "name": "index",
                "start_line": 1,
                "end_line": 1,
                "new_content": "A",
                "if_match": '"stale"',
            },
        )

    assert result.is_error is True
    assert _text(result) == "Error executing tool patch_page_lines: precondition failed; check if_match/if_none_match"


@pytest.mark.asyncio
async def test_patch_page_lines_404_returns_tool_error() -> None:
    """Read raises 404 → ``ToolError(\"page not found: {name}\")``."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="page not found")

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "patch_page_lines",
            {
                "name": "missing",
                "start_line": 1,
                "end_line": 1,
                "new_content": "X",
            },
        )

    assert result.is_error is True
    assert _text(result) == "Error executing tool patch_page_lines: page not found: missing"


@pytest.mark.asyncio
async def test_patch_page_lines_413_returns_tool_error_with_4_mib_wording() -> None:
    """Patched body > 4 MiB → ``ToolError(\"body too large: limit is 4 MiB\")``."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text="a\nb")
        return httpx.Response(413, text="body too large")

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "patch_page_lines",
            {
                "name": "index",
                "start_line": 1,
                "end_line": 2,
                "new_content": "x" * 1024,
            },
        )

    assert result.is_error is True
    assert _text(result) == "Error executing tool patch_page_lines: body too large: limit is 4 MiB"


@pytest.mark.asyncio
async def test_patch_page_lines_5xx_returns_tool_error() -> None:
    """SB 5xx surfaces the unified ``ToolError(\"silverbullet error: {status}\")`` wording.

    Either the read or the write can 5xx — the unified wording
    applies in both cases because both paths run through
    :func:`_translate_sb_errors`.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text="a\nb")
        return httpx.Response(502, text="bad gateway")

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "patch_page_lines",
            {
                "name": "index",
                "start_line": 1,
                "end_line": 1,
                "new_content": "A",
            },
        )

    assert result.is_error is True
    assert _text(result) == "Error executing tool patch_page_lines: silverbullet error: 502"


@pytest.mark.asyncio
async def test_patch_page_lines_returns_null_when_etag_header_missing() -> None:
    """A successful write with no ``ETag`` header → ``None``.

    Mirror of the same shape on ``write_page``, ``delete_page``, and
    ``append_to_page``: the wire payload is ``{\"result\": null}``
    and any future refactor that returned ``\"\"`` would be a
    confusing type drift for callers chaining edits.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text="a\nb")
        return httpx.Response(200)

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "patch_page_lines",
            {
                "name": "index",
                "start_line": 1,
                "end_line": 1,
                "new_content": "A",
            },
        )

    assert result.is_error is False
    assert result.structured_content == {"result": None}


# --- patch_page_replace -------------------------------------------------


@pytest.mark.asyncio
async def test_patch_page_replace_returns_etag_on_200() -> None:
    """Happy path: single literal match → read-modify-write round trip.

    Locks the read-modify-write shape: the tool reads first, then
    writes the replaced body, and returns the write's ETag (not
    the read's). The replaced body is asserted on directly so a
    future refactor that drops or duplicates content fails loudly.
    """
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            calls.append(("GET", request.url.path))
            return httpx.Response(200, text="hello world")
        calls.append(("PUT", request.url.path))
        assert request.content == b"hello SB"
        return httpx.Response(200, headers={"ETag": '"v2"'})

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "patch_page_replace",
            {"name": "index", "find": "world", "new_string": "SB"},
        )

    assert result.is_error is False
    assert _text(result) == '"v2"'
    # Read first, then write — locks the read-modify-write ordering
    # (same shape as ``append_to_page`` and ``patch_page_lines``).
    assert calls == [
        ("GET", "/.fs/index"),
        ("PUT", "/.fs/index"),
    ]


@pytest.mark.asyncio
async def test_patch_page_replace_default_replace_all_replaces_single_match() -> None:
    """With ``replace_all=False`` (default), a single occurrence is replaced.

    Locks the safe default: a unique substring match is replaced;
    the surrounding body is untouched. Contrast with
    :func:`test_patch_page_replace_multiple_matches_with_default_errors`
    (the multi-match-error path) and
    :func:`test_patch_page_replace_replace_all_true_replaces_every_match`
    (the explicit opt-in).
    """
    seen_writes: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text="hello world")
        seen_writes.append(request.content)
        return httpx.Response(200, headers={"ETag": '"v2"'})

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        await client.call_tool(
            "patch_page_replace",
            {"name": "index", "find": "world", "new_string": "SB"},
        )

    assert seen_writes == [b"hello SB"]


@pytest.mark.asyncio
async def test_patch_page_replace_multiple_matches_with_default_errors() -> None:
    """``replace_all=False`` + N > 1 matches → ``ToolError`` upfront.

    The read still happens (the tool can't know the match count
    without it), but the error surfaces *before* the write. The
    wording carries the match count so the caller can decide to
    narrow the find or opt into ``replace_all=True``.
    """
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        return httpx.Response(200, text="foo foo foo")

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "patch_page_replace",
            {"name": "index", "find": "foo", "new_string": "FOO"},
        )

    assert result.is_error is True
    assert _text(result) == (
        "Error executing tool patch_page_replace: "
        "find matched 3 times; pass replace_all=True or narrow find"
    )
    # Read happened (we needed it to count matches); no write.
    assert calls == ["GET"]


@pytest.mark.asyncio
async def test_patch_page_replace_multiple_matches_explicit_replace_all_false_errors() -> None:
    """``replace_all=False`` *explicitly* (same wording as default).

    Sanity check that explicit ``False`` doesn't accidentally change
    the behavior — the wording and the no-write outcome must match
    the default case.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="foo foo")

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "patch_page_replace",
            {
                "name": "index",
                "find": "foo",
                "new_string": "FOO",
                "replace_all": False,
            },
        )

    assert result.is_error is True
    assert _text(result) == (
        "Error executing tool patch_page_replace: "
        "find matched 2 times; pass replace_all=True or narrow find"
    )


@pytest.mark.asyncio
async def test_patch_page_replace_replace_all_true_replaces_every_match() -> None:
    """``replace_all=True`` → every occurrence is replaced."""
    seen_writes: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text="foo bar foo bar foo")
        seen_writes.append(request.content)
        return httpx.Response(200, headers={"ETag": '"v2"'})

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "patch_page_replace",
            {
                "name": "index",
                "find": "foo",
                "new_string": "FOO",
                "replace_all": True,
            },
        )

    assert result.is_error is False
    assert seen_writes == [b"FOO bar FOO bar FOO"]


@pytest.mark.asyncio
async def test_patch_page_replace_find_not_found_returns_tool_error() -> None:
    """Absent ``find`` → ``ToolError`` upfront.

    A silent no-op would mask a typo in the find string and look
    like success. Better to surface the miss loudly so the caller
    re-reads the page and corrects the typo. The read happens (we
    needed it to confirm the find is absent); no write.
    """
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        return httpx.Response(200, text="hello world")

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "patch_page_replace",
            {
                "name": "index",
                "find": "missing",
                "new_string": "X",
                "replace_all": True,  # even with all-match opt-in
            },
        )

    assert result.is_error is True
    assert _text(result) == (
        "Error executing tool patch_page_replace: find not found in body"
    )
    assert calls == ["GET"]


@pytest.mark.asyncio
async def test_patch_page_replace_empty_find_returns_tool_error() -> None:
    """``find=\"\"`` is rejected upfront — no read, no write.

    ``\"\".replace`` would match between every character (``\"abc\"``
    becomes ``\"XaXbXcX\"`` for ``new_string=\"X\"``), which is
    almost never what the caller wanted. Surfacing the bug loudly
    upfront saves the round trip and pinpoints the likely caller
    mistake.
    """
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        return httpx.Response(200, text="hello")

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "patch_page_replace",
            {"name": "index", "find": "", "new_string": "X"},
        )

    assert result.is_error is True
    assert _text(result) == (
        "Error executing tool patch_page_replace: find must not be empty"
    )
    # No GET, no PUT — the rejection is upfront, mirroring
    # ``append_to_page``'s empty-text guard.
    assert calls == []


@pytest.mark.asyncio
async def test_patch_page_replace_find_with_no_textual_overlap_returns_tool_error() -> None:
    """``replace_all=True`` + absent ``find`` still errors.

    The ``replace_all`` knob is about *how many* to replace, not
    *whether* to find one. The not-found error fires before the
    replace_all branch — so a caller who flips replace_all on
    blindly hoping to recover from a typo gets the same loud
    failure they would have gotten with the default.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="abc")

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "patch_page_replace",
            {
                "name": "index",
                "find": "xyz",
                "new_string": "FOO",
                "replace_all": True,
            },
        )

    assert result.is_error is True
    assert _text(result) == (
        "Error executing tool patch_page_replace: find not found in body"
    )


@pytest.mark.asyncio
async def test_patch_page_replace_treats_find_as_literal_not_regex() -> None:
    """``find`` is matched as a literal substring, no regex semantics.

    ``\b`` and ``\\d`` are characters, not regex anchors / character
    classes. A caller who wants regex matches calls ``rg`` or
    Python ``re`` client-side first, then patches with the literal
    result — the bridge doesn't escape for them (escaping
    silently invites \"I forgot to escape\" disasters).
    """
    seen_writes: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text="the \\d placeholder")
        seen_writes.append(request.content)
        return httpx.Response(200, headers={"ETag": '"v2"'})

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        await client.call_tool(
            "patch_page_replace",
            {
                "name": "index",
                "find": "\\d",
                "new_string": "X",
                "replace_all": True,
            },
        )

    # Literal ``\\d`` (two characters: backslash + d) gets replaced
    # with ``X`` — NOT regex ``\\d`` (which would mean \"any digit\").
    assert seen_writes == [b"the X placeholder"]


@pytest.mark.asyncio
async def test_patch_page_replace_empty_new_string_deletes_occurrences() -> None:
    """``new_string=\"\"`` deletes the matched substrings.

    No separate ``delete_occurrences`` tool needed — same
    standing-preference rule as ``patch_page_lines``'s empty
    ``new_content`` deletes the range. The result is the body with
    every match removed.
    """
    seen_writes: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text="abcdefg")
        seen_writes.append(request.content)
        return httpx.Response(200, headers={"ETag": '"v2"'})

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        await client.call_tool(
            "patch_page_replace",
            {
                "name": "index",
                "find": "cd",
                "new_string": "",
            },
        )

    # ``cd`` is a single match → ``replace_all=False`` succeeds with
    # a deletion. Result is ``abefg``.
    assert seen_writes == [b"abefg"]


@pytest.mark.asyncio
async def test_patch_page_replace_find_spanning_newlines_works() -> None:
    """The ``find`` substring can span line breaks.

    ``str.replace`` is substring-based, not line-based; the body
    is a flat string from the bridge's perspective. A match that
    straddles a ``\\n`` is fine — this differs from
    ``patch_page_lines``'s line-indexed shape intentionally.
    """
    seen_writes: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text="line 1\nline 2\nline 3")
        seen_writes.append(request.content)
        return httpx.Response(200, headers={"ETag": '"v2"'})

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        await client.call_tool(
            "patch_page_replace",
            {
                "name": "index",
                "find": "1\nline 2",
                "new_string": "X",
            },
        )

    assert seen_writes == [b"line X\nline 3"]


@pytest.mark.asyncio
async def test_patch_page_replace_forwards_if_match_to_write() -> None:
    """``if_match`` is forwarded to the PUT, not the GET.

    Same contract as ``append_to_page`` and ``patch_page_lines``:
    the precondition guards the *write*, not the read. A stale
    etag from the caller's last ``read_page`` is the typical
    use case.
    """
    seen_if_match: list[str] = []
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        seen_if_match.append(request.headers.get("If-Match", ""))
        if request.method == "GET":
            return httpx.Response(200, text="hello world")
        return httpx.Response(200, headers={"ETag": '"v2"'})

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        await client.call_tool(
            "patch_page_replace",
            {
                "name": "index",
                "find": "world",
                "new_string": "SB",
                "if_match": '"v1"',
            },
        )

    # Read first, then write; If-Match only on the write.
    assert calls == ["GET", "PUT"]
    assert seen_if_match == ["", '"v1"']


@pytest.mark.asyncio
async def test_patch_page_replace_forwards_if_match_star() -> None:
    """``if_match=\"*\"`` requires the page to exist (the write layer checks).

    Coherent with ``append_to_page``'s ``if_match=\"*\"`` semantics:
    the read happens unconditionally, the write carries
    ``If-Match: *``. (The read here is for counting matches; the
    precondition is on the write.)
    """
    seen_if_match: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text="hello world")
        seen_if_match.append(request.headers.get("If-Match", ""))
        return httpx.Response(200, headers={"ETag": '"v2"'})

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        await client.call_tool(
            "patch_page_replace",
            {
                "name": "index",
                "find": "world",
                "new_string": "SB",
                "if_match": "*",
            },
        )

    assert seen_if_match == ["*"]


@pytest.mark.asyncio
async def test_patch_page_replace_stale_if_match_returns_412_tool_error() -> None:
    """Stale ``if_match`` on the PUT → ``ToolError(\"precondition failed; …\")``."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text="hello world")
        return httpx.Response(412, text="precondition failed")

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "patch_page_replace",
            {
                "name": "index",
                "find": "world",
                "new_string": "SB",
                "if_match": '"stale"',
            },
        )

    assert result.is_error is True
    assert _text(result) == (
        "Error executing tool patch_page_replace: "
        "precondition failed; check if_match/if_none_match"
    )


@pytest.mark.asyncio
async def test_patch_page_replace_404_returns_tool_error() -> None:
    """Read raises 404 → ``ToolError(\"page not found: {name}\")``."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="page not found")

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "patch_page_replace",
            {
                "name": "missing",
                "find": "anything",
                "new_string": "X",
            },
        )

    assert result.is_error is True
    assert _text(result) == (
        "Error executing tool patch_page_replace: page not found: missing"
    )


@pytest.mark.asyncio
async def test_patch_page_replace_413_returns_tool_error_with_4_mib_wording() -> None:
    """Replaced body > 4 MiB → ``ToolError(\"body too large: limit is 4 MiB\")``."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text="hello world")
        return httpx.Response(413, text="body too large")

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "patch_page_replace",
            {
                "name": "index",
                "find": "world",
                "new_string": "X",
            },
        )

    assert result.is_error is True
    assert _text(result) == (
        "Error executing tool patch_page_replace: body too large: limit is 4 MiB"
    )


@pytest.mark.asyncio
async def test_patch_page_replace_5xx_returns_tool_error() -> None:
    """SB 5xx surfaces the unified ``ToolError(\"silverbullet error: {status}\")`` wording.

    Either the read or the write can 5xx — the unified wording
    applies in both cases because both paths run through
    :func:`_translate_sb_errors`.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text="hello world")
        return httpx.Response(502, text="bad gateway")

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "patch_page_replace",
            {
                "name": "index",
                "find": "world",
                "new_string": "SB",
            },
        )

    assert result.is_error is True
    assert _text(result) == (
        "Error executing tool patch_page_replace: silverbullet error: 502"
    )


@pytest.mark.asyncio
async def test_patch_page_replace_returns_null_when_etag_header_missing() -> None:
    """A successful write with no ``ETag`` header → ``None``.

    Mirror of the same shape on the other ``str | None`` returns
    (``write_page``, ``delete_page``, ``append_to_page``,
    ``patch_page_lines``): the wire payload is
    ``{\"result\": null}`` and any future refactor that returned
    ``\"\"`` would be a confusing type drift for callers chaining
    edits.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text="hello world")
        return httpx.Response(200)

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "patch_page_replace",
            {
                "name": "index",
                "find": "world",
                "new_string": "SB",
            },
        )

    assert result.is_error is False
    assert result.structured_content == {"result": None}


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

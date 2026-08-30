"""Layer 1 tests for ``server.py``: in-memory ``Client(mcp)`` with a
mocked SB transport.

We substitute ``httpx.MockTransport`` for a real SilverBullet so the
test suite never needs a running SB. The full HTTP integration matrix
(bridge asgi on a real socket + real auth + discovery doc) lives in
``tests/test_http_auth`` (Layer 2), built on top of this module.

Coverage:

- All nine ``/.fs``-backed tools (``read_page``, ``page_exists``,
  ``write_page``, ``delete_page``, ``append_to_page``,
  ``patch_page_lines``, ``patch_page_replace``, ``move_page``,
  ``list_pages``) on the 200 happy path;
  ``write_page`` / ``delete_page`` / ``append_to_page`` /
  ``patch_page_lines`` / ``patch_page_replace`` return the ETag,
  ``list_pages`` returns the file metas, ``read_page`` and the
  resource template both surface the markdown body, ``page_exists``
  returns ``bool`` (T25). ``append_to_page`` (T19), ``patch_page_lines``
  (T20), ``patch_page_replace`` (T21), and ``move_page`` (T22) are
  the read-modify-write / write-then-delete tools.
- ``write_page`` carries the ``if_match`` straight through to
  ``sb_client`` (T3 covers the wire envelope; this test guards the
  MCP-tool-to-SB-client argument path).
- ``list_pages`` filters by prefix client-side.
- Each SB exception maps to ``is_error=True`` with the design doc's
  exact ToolError message: 404 → "page not found: <name>"; 412 →
  "precondition failed; check if_match/if_none_match"; 413 →
  "body too large: limit is 4 MiB"; 5xx → "silverbullet error: <status>";
  timeout → "silverbullet request timed out". The eight tools share
  the translation through :func:`server._translate_sb_errors`;
  ``page_exists`` (T25) translates 5xx and timeout inline because
  404 is the *answer* (not an error) for the existence question
  — a different exception-translation contract on the ninth tool.
- The resource template returns the same body for the happy path and
  surfaces ``ToolError`` for a missing page (v1 keeps one error shape
  for both surfaces; T4 carry-forward note in the map).

T22 (``move_page``) is the eighth tool: write-then-delete rename
with destination-collision and atomicity-caveat error wording
distinct from the unified 412/404 shapes. T25 (``page_exists``)
adds the ninth tool: a cheap ``bool`` existence check that doesn't
go through :func:`server._translate_sb_errors` because 404 is the
*answer*, not an error.
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
async def test_read_page_returns_ack_envelope_on_200() -> None:
    """``read_page`` returns the T24 acknowledgement envelope.

    v1.1 returned ``str`` (just the markdown body). v1.2 T24 widens
    the return to ``{body, etag, size_bytes, last_modified_ms}``
    so an agent that just read a page knows its etag (for an
    ``if_match`` round-trip on the next write) and its current
    size without a follow-up call. ``name`` is dropped (the caller
    already passed it in) and ``created_ms`` is dropped (reads have
    no create-vs-update distinction to surface); the full meta
    envelope lives on :class:`PageMeta` so a future
    wider-it-still ticket is a one-liner.

    Locks the read-tool wire shape — the ``silverbullet://page/{name}``
    resource template returns the same envelope, so an agent that
    gets the same dict from both surfaces (tool call vs context
    attachment) can treat them identically.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/.fs/index"
        return httpx.Response(
            200,
            text="# hello",
            headers={
                "ETag": '"abc123"',
                "X-Last-Modified": "1700000000123",
                "X-Content-Length": "7",
            },
        )

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("read_page", {"name": "index"})

    assert result.is_error is False
    # ``size_bytes`` comes from SB's ``X-Content-Length`` response
    # header. ``created_ms`` is intentionally absent (not even
    # ``None``) — T24 drops the field, so the wire payload mirrors
    # that. ``name`` is also dropped (caller already knows it).
    # The SDK surfaces the dict return under ``structured_content``
    # because the handler's return annotation is
    # ``dict[str, object]``.
    assert result.structured_content == {
        "body": "# hello",
        "etag": '"abc123"',
        "size_bytes": 7,
        "last_modified_ms": 1700000000123,
    }


@pytest.mark.asyncio
async def test_read_page_ack_envelope_is_none_when_meta_stripped() -> None:
    """A 200 with no ``X-*`` / ``ETag`` headers → envelope with Nones for meta.

    Mirror of the v1.1 None-ETag handling on the T24 envelope:
    a proxy-stripped response surfaces ``None`` for every optional
    meta field. ``body`` is still the markdown text (always
    populated on a 200).
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="body")

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("read_page", {"name": "index"})

    assert result.is_error is False
    assert result.structured_content == {
        "body": "body",
        "etag": None,
        "size_bytes": None,
        "last_modified_ms": None,
    }


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


# --- page_exists -------------------------------------------------------


@pytest.mark.asyncio
async def test_page_exists_returns_true_on_200() -> None:
    """``page_exists`` returns ``True`` on 200 — the existence is confirmed.

    T25: cheap existence check that costs one ``GET /.fs/{name}``
    round trip without materializing the body. Locks the
    ``bool`` return type and the success path so a future
    refactor that swaps it for a richer envelope doesn't
    silently widen the wire shape.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/.fs/index"
        return httpx.Response(200, text="# body")

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("page_exists", {"name": "index"})

    assert result.is_error is False
    # ``page_exists`` returns a Python ``bool``; the SDK wraps
    # single-value returns in ``{"result": ...}`` for
    # ``structured_content`` (the same shape as ``list_pages``'s
    # list return). The agent reads ``structured_content["result"]``
    # to get the boolean.
    assert result.structured_content == {"result": True}


@pytest.mark.asyncio
async def test_page_exists_returns_false_on_404() -> None:
    """``page_exists`` returns ``False`` on 404 — not a ``ToolError``.

    T25: 404 is the existence question's "no" answer, *not* an
    error. Compare with :func:`test_read_page_404_returns_tool_error_with_design_doc_wording`
    — same upstream status, different wire shape, different
    tool. The two shapes coexist because the questions are
    different: "give me the body" (read) and "is it there?"
    (exists).
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="page not found")

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("page_exists", {"name": "missing"})

    assert result.is_error is False
    assert result.structured_content == {"result": False}


@pytest.mark.asyncio
async def test_page_exists_5xx_returns_tool_error() -> None:
    """5xx surfaces as ``ToolError("silverbullet error: {status}")``.

    T25 deliberately returns an error (not ``False``) on 5xx: "I
    don't know, the server is broken" is not a valid "no". An
    agent that gets ``False`` proceeds with confidence; an agent
    that gets a tool error retries or surfaces the failure. Same
    wording as the other tools so the agent's error-handling
    doesn't have to special-case ``page_exists``.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="upstream gone")

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("page_exists", {"name": "anything"})

    assert result.is_error is True
    assert _text(result) == "Error executing tool page_exists: silverbullet error: 503"


@pytest.mark.asyncio
async def test_page_exists_timeout_returns_tool_error() -> None:
    """Timeout surfaces as ``ToolError("silverbullet request timed out")``.

    Locks the timeout wording on the existence tool to match the
    rest of the bridge. ``exists_page`` lets ``httpx.TimeoutException``
    propagate (the SB client doesn't translate timeouts — that's
    the MCP layer's job); the tool handler catches and translates
    it inline, mirroring :func:`_translate_sb_errors` minus the
    404 clause.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("simulated")

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("page_exists", {"name": "anything"})

    assert result.is_error is True
    assert _text(result) == "Error executing tool page_exists: silverbullet request timed out"


@pytest.mark.asyncio
async def test_page_exists_412_returns_precondition_tool_error() -> None:
    """A GET that 412s surfaces the unified 412 ``ToolError``.

    ``exists_page`` issues ``GET /.fs/{name}`` and a 412 on a GET
    is unusual (preconditions live on writes), but the SB client
    surfaces any 412 as :class:`PreconditionFailed` and the tool
    handler translates it with the same wording as the other
    tools. Missing this case would mean a proxy / SB
    misconfiguration leaves an unhandled exception, which the SDK
    surfaces as a generic ``MCPError`` without the design-doc
    wording.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(412, text="precondition failed")

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("page_exists", {"name": "index"})

    assert result.is_error is True
    assert (
        _text(result)
        == "Error executing tool page_exists: precondition failed; check if_match/if_none_match"
    )


@pytest.mark.asyncio
async def test_page_exists_does_not_materialize_body() -> None:
    """The handler returns a body; ``page_exists`` doesn't surface it.

    Locks the cost-down promise: a body-returning 200 should not
    make the MCP tool's structured content grow a body field. If
    a future refactor swaps ``exists_page`` for ``read_page`` and
    returns the read-side envelope, the wire shape silently widens
    and a caller expecting a ``bool`` breaks.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text="# body that must not leak",
            headers={"ETag": '"abc"', "X-Content-Length": "27"},
        )

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("page_exists", {"name": "index"})

    assert result.is_error is False
    # ``bool`` is the wire shape. Nothing else.
    assert result.structured_content == {"result": True}


# --- write_page --------------------------------------------------------


@pytest.mark.asyncio
async def test_write_page_returns_ack_envelope_on_200() -> None:
    """``write_page`` returns the T23 acknowledgement envelope.

    v1.1 returned ``str`` (the new ETag) or ``None` (older SB /
    proxy-stripped). v1.2 T23 widens the return to
    ``{name, etag, size_bytes, last_modified_ms, created_ms}`` so
    the agent knows how big the body is, when it was written, and
    whether the write was a create vs an update without a
    follow-up read.

    Locks the write-tool wire shape — every write tool (the seven
    in ``/.fs``) returns this same envelope per T23.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={
                "ETag": '"abc123"',
                "X-Last-Modified": "1700000000123",
                "X-Created": "1700000000000",
            },
        )

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "write_page",
            {"name": "index", "content": "# new body"},
        )

    assert result.is_error is False
    # ``size_bytes`` is the UTF-8 byte count of the just-written body
    # (``# new body`` = 10 bytes), surfaced from the request body.
    # The SDK surfaces the dict return directly under
    # ``structured_content`` (not wrapped in ``{"result": …}`` the
    # way single-value returns are — a dict return IS the
    # structured payload).
    assert result.structured_content == {
        "name": "index",
        "etag": '"abc123"',
        "size_bytes": 10,
        "last_modified_ms": 1700000000123,
        "created_ms": 1700000000000,
    }


@pytest.mark.asyncio
async def test_write_page_ack_envelope_is_none_when_meta_stripped() -> None:
    """A 200 with no ``X-*`` / ``ETag`` headers → ack envelope with Nones.

    Mirror of the v1.1 None-ETag handling on the wider T23 envelope:
    a proxy-stripped response surfaces ``None`` for every optional
    meta field (etag / last_modified_ms / created_ms). ``size_bytes``
    is still populated (request-body derivation — the bridge always
    knows how many bytes it wrote).
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
    assert result.structured_content == {
        "name": "index",
        "etag": None,
        "size_bytes": 10,
        "last_modified_ms": None,
        "created_ms": None,
    }


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
async def test_delete_page_returns_ack_envelope_on_200() -> None:
    """``delete_page`` returns the T23 acknowledgement envelope.

    DELETE doesn't echo ``X-*`` meta per the design doc, so the
    envelope's size / timestamp fields are ``None`` (the honest
    answer — we don't fabricate them). The ETag echoes the deleted
    body's hash so the caller can confirm what was removed. An agent
    that wants the timestamps of what it's about to delete reads
    the page first and threads the etag into ``if_match``.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        assert request.url.path == "/.fs/index"
        return httpx.Response(200, headers={"ETag": '"abc123"'})

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("delete_page", {"name": "index"})

    assert result.is_error is False
    assert result.structured_content == {
        "name": "index",
        "etag": '"abc123"',
        "size_bytes": None,
        "last_modified_ms": None,
        "created_ms": None,
    }


@pytest.mark.asyncio
async def test_delete_page_ack_envelope_when_etag_header_missing() -> None:
    """No ``ETag`` on the response → envelope with etag ``None``.

    Same envelope shape as the success case; only ``etag`` differs.
    Locks the DELETE envelope so a future refactor that drops one of
    the ``None`` fields changes the wire shape loudly.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200)

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("delete_page", {"name": "index"})

    assert result.is_error is False
    assert result.structured_content == {
        "name": "index",
        "etag": None,
        "size_bytes": None,
        "last_modified_ms": None,
        "created_ms": None,
    }


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
async def test_append_to_page_returns_ack_envelope_on_200() -> None:
    """Happy path: existing body + new text → read-modify-write round trip.

    Captures the GET (read) and PUT (write) the tool issues so we
    can assert: (a) the read happened first, (b) the write carries
    the combined body, (c) the tool returns the write's T23 ack
    envelope (not the read's). The envelope's ``size_bytes`` is the
    UTF-8 byte count of the just-written combined body
    (``hello\nworld`` = 11 bytes).
    """
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            calls.append(("GET", request.url.path))
            return httpx.Response(200, text="hello\n")
        # PUT
        calls.append(("PUT", request.url.path))
        assert request.content == b"hello\nworld"
        return httpx.Response(
            200,
            headers={
                "ETag": '"v2"',
                "X-Last-Modified": "1700000000123",
                "X-Created": "1700000000000",
            },
        )

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "append_to_page",
            {"name": "index", "text": "world"},
        )

    assert result.is_error is False
    assert result.structured_content == {
        "name": "index",
        "etag": '"v2"',
        "size_bytes": 11,  # ``hello\nworld`` = 11 UTF-8 bytes
        "last_modified_ms": 1700000000123,
        "created_ms": 1700000000000,
    }
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
async def test_append_to_page_ack_envelope_when_etag_header_missing() -> None:
    """A successful write with no ``ETag`` / ``X-*`` headers → ack envelope with Nones.

    Mirror of :func:`test_write_page_ack_envelope_is_none_when_meta_stripped`
    on the append path. ``size_bytes`` is still populated from the
    request body (the combined body the bridge just wrote:
    ``hello\nworld`` = 11 bytes).
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
    assert result.structured_content == {
        "name": "index",
        "etag": None,
        "size_bytes": 11,
        "last_modified_ms": None,
        "created_ms": None,
    }


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
    assert result.structured_content == {
        "name": "index",
        "etag": '"v2"',
        "size_bytes": 7,  # ``a\nB\nC\nd`` = 7 UTF-8 bytes
        "last_modified_ms": None,
        "created_ms": None,
    }
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
async def test_patch_page_lines_ack_envelope_when_etag_header_missing() -> None:
    """A successful write with no ``ETag`` / ``X-*`` headers → ack envelope with Nones.

    Mirror of :func:`test_write_page_ack_envelope_is_none_when_meta_stripped`
    on the patch-lines path. ``size_bytes`` is the just-written
    patched body (``A\nb`` = 3 UTF-8 bytes — line 1 replaced with
    ``A``, line 2 left as ``b``).
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
    assert result.structured_content == {
        "name": "index",
        "etag": None,
        "size_bytes": 3,
        "last_modified_ms": None,
        "created_ms": None,
    }


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
    assert result.structured_content == {
        "name": "index",
        "etag": '"v2"',
        "size_bytes": 8,  # ``hello SB`` = 8 UTF-8 bytes
        "last_modified_ms": None,
        "created_ms": None,
    }
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
async def test_patch_page_replace_ack_envelope_when_etag_header_missing() -> None:
    """A successful write with no ``ETag`` / ``X-*`` headers → ack envelope with Nones.

    Mirror of :func:`test_write_page_ack_envelope_is_none_when_meta_stripped`
    on the patch-replace path. ``size_bytes`` is the just-written
    replaced body (``hello SB`` = 8 UTF-8 bytes).
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
    assert result.structured_content == {
        "name": "index",
        "etag": None,
        "size_bytes": 8,
        "last_modified_ms": None,
        "created_ms": None,
    }


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


# --- move_page --------------------------------------------------------


@pytest.mark.asyncio
async def test_move_page_returns_new_etag_on_200() -> None:
    """Happy path: read → write new → delete old, return new etag.

    Locks the write-then-delete ordering (the ticket's atomicity
    choice — write first so a partial-failure case leaves the body
    at the new name rather than losing it). The new page's ETag
    is returned; the source's ETag is discarded.
    """
    calls: list[tuple[str, str, bytes]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.content if request.method != "GET" else b""
        calls.append((request.method, request.url.path, body))
        if request.method == "GET":
            assert request.url.path == "/.fs/old"
            return httpx.Response(200, text="the body\n")
        if request.method == "PUT":
            assert request.url.path == "/.fs/new"
            # If-None-Match: * on the destination write — move is
            # rename, never silently overwrite.
            assert request.headers.get("If-None-Match") == "*"
            assert request.content == b"the body\n"
            return httpx.Response(200, headers={"ETag": '"new-etag"'})
        # DELETE
        assert request.url.path == "/.fs/old"
        return httpx.Response(200, headers={"ETag": '"old-etag"'})

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "move_page", {"name": "old", "new_name": "new"}
        )

    assert result.is_error is False
    assert result.structured_content == {
        "name": "new",  # destination, not source — per T23
        "etag": '"new-etag"',
        "size_bytes": 9,  # ``the body\n`` = 9 UTF-8 bytes
        "last_modified_ms": None,
        "created_ms": None,
    }
    # Order: GET source → PUT destination (with If-None-Match) →
    # DELETE source. The write happens before the delete so a
    # partial-failure leaves the body at the new name.
    assert [(m, p) for m, p, _ in calls] == [
        ("GET", "/.fs/old"),
        ("PUT", "/.fs/new"),
        ("DELETE", "/.fs/old"),
    ]
    # The body written to the destination matches what was read.
    assert calls[1][2] == b"the body\n"


@pytest.mark.asyncio
async def test_move_page_same_name_is_no_op() -> None:
    """``name == new_name`` short-circuits: read for existence, no PUT/DELETE.

    Avoids the read-write-delete cycle (and the spurious 412 that
    the dance would invite — we'd just write a fresh body to the
    same path, making the etag from the read stale for the delete
    that follows). T23: the same-name no-op now returns the page's
    full acknowledgement envelope (etags, size, timestamps) since
    the underlying ``read_page`` already populates it — no
    extra round trip, just a one-line unwrap on the existing
    read response.
    """
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        return httpx.Response(
            200,
            text="body",
            headers={"ETag": '"self-etag"', "X-Content-Length": "4"},
        )

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "move_page", {"name": "self", "new_name": "self"}
        )

    assert result.is_error is False
    # Only the existence check ran.
    assert calls == [("GET", "/.fs/self")]
    # The same-name no-op returns the page's read-side ack — the
    # caller gets the size / etag / timestamps without an extra
    # round trip.
    assert result.structured_content == {
        "name": "self",
        "etag": '"self-etag"',
        "size_bytes": 4,
        "last_modified_ms": None,
        "created_ms": None,
    }


@pytest.mark.asyncio
async def test_move_page_same_name_missing_returns_404_tool_error() -> None:
    """Same-name short-circuit on a missing page surfaces 404 wording.

    The short-circuit still needs to verify existence — silently
    succeeding on a missing page would mask a caller typo.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="page not found")

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "move_page", {"name": "ghost", "new_name": "ghost"}
        )

    assert result.is_error is True
    assert _text(result) == "Error executing tool move_page: page not found: ghost"


@pytest.mark.asyncio
async def test_move_page_forwards_if_match_to_delete() -> None:
    """``if_match`` is threaded into the source DELETE, not the GET.

    Mirrors the append/patch contract: ``if_match`` guards the
    *write* side (here the source delete — the etag from the read
    is the natural anchor). The read carries no precondition; the
    destination PUT carries ``If-None-Match: *`` (move is rename,
    not merge).
    """
    seen_if_match: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_if_match.append(request.headers.get("If-Match", ""))
        if request.method == "GET":
            return httpx.Response(200, text="body")
        if request.method == "PUT":
            return httpx.Response(200, headers={"ETag": '"new"'})
        return httpx.Response(200, headers={"ETag": '"old"'})

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        await client.call_tool(
            "move_page",
            {"name": "old", "new_name": "new", "if_match": '"v1"'},
        )

    # GET has no precondition; PUT has If-None-Match (not If-Match);
    # DELETE carries the caller's if_match.
    assert seen_if_match == ["", "", '"v1"']


@pytest.mark.asyncio
async def test_move_page_404_on_read_returns_tool_error() -> None:
    """Source missing on the read surfaces the standard 404 wording."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="page not found")

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "move_page", {"name": "missing", "new_name": "new"}
        )

    assert result.is_error is True
    assert _text(result) == "Error executing tool move_page: page not found: missing"


@pytest.mark.asyncio
async def test_move_page_destination_exists_returns_destination_collision_error() -> None:
    """Destination PUT 412 (from ``If-None-Match: *``) → collision wording.

    Distinct from the unified 412 wording: the caller asked to move
    a *different* page to ``new_name`` and the destination already
    exists. Saying "page not found: {name}" would be wrong;
    saying just "precondition failed" would be ambiguous (source
    or destination?). The destination-collision message names the
    destination and refuses, period.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text="body")
        # PUT to /new — already exists.
        return httpx.Response(412, text="precondition failed")

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "move_page", {"name": "old", "new_name": "new"}
        )

    assert result.is_error is True
    assert (
        _text(result)
        == "Error executing tool move_page: destination page already exists: new; refusing to overwrite"
    )


@pytest.mark.asyncio
async def test_move_page_delete_412_returns_atomicity_caveat_error() -> None:
    """Source DELETE 412 after a successful write → atomicity-caveat wording.

    The destination already has the body; the source couldn't be
    deleted (probably because its etag went stale — concurrent
    edit between the read and the delete). Both names now point
    at a page; the caller needs to clean up. The error names both
    names and tells the caller to delete the duplicate.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text="body")
        if request.method == "PUT":
            return httpx.Response(200, headers={"ETag": '"new"'})
        # DELETE fails 412 — source etag stale.
        return httpx.Response(412, text="precondition failed")

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "move_page", {"name": "old", "new_name": "new"}
        )

    assert result.is_error is True
    assert (
        _text(result)
        == "Error executing tool move_page: moved body to new but failed to delete old: precondition failed; check if_match/if_none_match; both now exist"
    )


@pytest.mark.asyncio
async def test_move_page_delete_404_surfaces_atomicity_message_not_generic_404() -> None:
    """Source DELETE 404 (deleted between read and delete) → atomicity message.

    The body is at ``new_name`` already — that's what the caller
    wanted. The source going missing during cleanup is a feature
    (someone else deleted it for us), not a bug. The generic
    "page not found: old" wording would be misleading.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text="body")
        if request.method == "PUT":
            return httpx.Response(200, headers={"ETag": '"new"'})
        return httpx.Response(404, text="page not found")

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "move_page", {"name": "old", "new_name": "new"}
        )

    assert result.is_error is True
    assert (
        _text(result)
        == "Error executing tool move_page: moved body to new but old was already deleted before the cleanup step"
    )


@pytest.mark.asyncio
async def test_move_page_delete_5xx_returns_atomicity_caveat_error() -> None:
    """Source DELETE 5xx after a successful write → atomicity-caveat wording.

    Server-side failure during cleanup. The body is at both names;
    the caller needs to know to retry the delete (or recover
    manually).
    """
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text="body")
        if request.method == "PUT":
            return httpx.Response(200, headers={"ETag": '"new"'})
        return httpx.Response(502, text="bad gateway")

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "move_page", {"name": "old", "new_name": "new"}
        )

    assert result.is_error is True
    assert (
        _text(result)
        == "Error executing tool move_page: moved body to new but failed to delete old: silverbullet error: 502; both now exist"
    )


@pytest.mark.asyncio
async def test_move_page_delete_timeout_returns_atomicity_caveat_error() -> None:
    """Source DELETE timeout after a successful write → atomicity-caveat wording.

    Timeouts during cleanup leave the body at both names; the
    caller retries or recovers manually.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text="body")
        if request.method == "PUT":
            return httpx.Response(200, headers={"ETag": '"new"'})
        raise httpx.ReadTimeout("simulated")

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "move_page", {"name": "old", "new_name": "new"}
        )

    assert result.is_error is True
    assert (
        _text(result)
        == "Error executing tool move_page: moved body to new but failed to delete old: silverbullet request timed out; both now exist"
    )


@pytest.mark.asyncio
async def test_move_page_destination_write_413_returns_tool_error() -> None:
    """Destination write 413 → standard body-too-large wording.

    The body read from the source exceeded SB's 4 MiB limit when
    re-PUT to the destination. The source isn't deleted (the
    PUT failed before the DELETE ran), so the body still lives at
    ``name``. Standard wording — the partial-failure shape is
    "destination write failed", not "source cleanup failed", so
    the atomicity message doesn't apply.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text="body")
        return httpx.Response(413, text="body too large")

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "move_page", {"name": "old", "new_name": "new"}
        )

    assert result.is_error is True
    assert (
        _text(result)
        == "Error executing tool move_page: body too large: limit is 4 MiB"
    )


@pytest.mark.asyncio
async def test_move_page_read_5xx_returns_tool_error() -> None:
    """Source read 5xx surfaces the unified 5xx wording."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="upstream gone")

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "move_page", {"name": "old", "new_name": "new"}
        )

    assert result.is_error is True
    assert _text(result) == "Error executing tool move_page: silverbullet error: 503"


@pytest.mark.asyncio
async def test_move_page_ack_envelope_when_new_etag_header_missing() -> None:
    """Destination write 200 with no ETag / ``X-*`` headers → ack envelope with Nones.

    Mirror of the same shape on the other write tools. The wire
    payload is the destination's T23 ack envelope (``name=new``
    because the destination is what we wrote and the caller wants
    to know about). ``size_bytes`` is still populated from the
    request body (``body`` = 4 UTF-8 bytes).
    """
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text="body")
        if request.method == "PUT":
            return httpx.Response(200)
        return httpx.Response(200, headers={"ETag": '"old"'})

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "move_page", {"name": "old", "new_name": "new"}
        )

    assert result.is_error is False
    assert result.structured_content == {
        "name": "new",  # destination
        "etag": None,
        "size_bytes": 4,
        "last_modified_ms": None,
        "created_ms": None,
    }


@pytest.mark.asyncio
async def test_move_page_does_not_delete_on_destination_collision() -> None:
    """When the destination write fails, the source is not deleted.

    The atomicity story is "write first, then delete". If the
    write fails (412 from ``If-None-Match: *``), the source
    DELETE never runs — the source is untouched. This pins down
    the ordering so a future refactor that moved the DELETE
    before the PUT (or ran them concurrently) doesn't silently
    lose data on a destination collision.
    """
    methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        if request.method == "GET":
            return httpx.Response(200, text="body")
        # Destination collision.
        return httpx.Response(412, text="precondition failed")

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "move_page", {"name": "old", "new_name": "new"}
        )

    assert result.is_error is True
    # Read, then failed PUT — no DELETE.
    assert methods == ["GET", "PUT"]


# --- resource template -------------------------------------------------


@pytest.mark.asyncio
async def test_resource_template_returns_ack_envelope() -> None:
    """``silverbullet://page/{name}`` returns the T24 ack envelope.

    v1.1 returned a raw markdown string (``text/markdown``). v1.2
    T24 widens the resource to return the same
    ``{body, etag, size_bytes, last_modified_ms}`` envelope as the
    read tool, JSON-serialized into the SDK's resource text field;
    the MIME type is ``application/json`` because the value is a
    structured object, not raw markdown. Callers parse
    ``content.text`` as JSON to read the envelope (or
    ``json.loads(content.text)["body"]`` to grab the markdown).

    Locks the resource wire shape; the read tool's envelope is
    tested separately above.
    """
    import json

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/.fs/index"
        return httpx.Response(
            200,
            text="# page body",
            headers={
                "ETag": '"abc123"',
                "X-Last-Modified": "1700000000123",
                "X-Content-Length": "11",
            },
        )

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.read_resource("silverbullet://page/index")

    assert len(result.contents) == 1
    content = result.contents[0]
    # MIME type flipped from ``text/markdown`` (v1.1 raw body) to
    # ``application/json`` (v1.2 structured envelope). The body
    # lives inside the JSON, not as raw text.
    assert getattr(content, "mime_type", None) == "application/json"
    assert json.loads(content.text) == {
        "body": "# page body",
        "etag": '"abc123"',
        "size_bytes": 11,
        "last_modified_ms": 1700000000123,
    }


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

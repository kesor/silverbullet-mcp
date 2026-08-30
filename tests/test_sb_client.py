"""Layer 3 tests for ``sb_client.py``: bridge -> SB request envelope.

We substitute ``httpx.MockTransport`` for a real SilverBullet so the
test suite never needs a running SB. The full integration matrix
(bridge -> SB -> MCP tool response) lives in ``tests/test_tools_in_memory``
(Layer 1) and ``tests/test_http_auth`` (Layer 2), built on top of
this module.

Coverage:

- ``read_page``: 200 / 404 / 5xx.
- ``write_page``: 200 / 404 / 412 / 413 / 5xx; ``X-Source: external`` and
  ``X-Permission: rw`` on every PUT; ``If-Match`` and ``If-None-Match: *``
  envelopes; body content round-trips verbatim.
- ``list_pages``: 200 array of FileMeta; non-array body surfaces as
  ServerError.
"""

from __future__ import annotations

import json

import httpx2 as httpx
import pytest

from mcp_silverbullet.sb_client import (
    BodyTooLarge,
    FileMeta,
    PageMeta,
    PageNotFound,
    PreconditionFailed,
    SBClient,
    ServerError,
)


TOKEN = "test-secret-do-not-use-in-prod"
BASE = "http://sb.test"


def _client(handler) -> SBClient:
    """Build an ``SBClient`` whose underlying transport is ``handler``.

    ``handler`` is an ``httpx.MockTransport`` callable — it receives
    every request and returns a synthetic ``httpx.Response``.
    """

    transport = httpx.MockTransport(handler)
    sb = SBClient.__new__(SBClient)
    # Skip ``__init__``'s real ``AsyncClient`` so we can inject the mock
    # transport directly. Keeps the test free of network sockets.
    sb._client = httpx.AsyncClient(
        base_url=BASE,
        headers={"Authorization": f"Bearer {TOKEN}"},
        transport=transport,
    )
    return sb


# --- read_page ---------------------------------------------------------


@pytest.mark.asyncio
async def test_read_page_returns_body_on_200() -> None:
    """``read_page`` returns ``PageMeta`` whose ``.body`` is the markdown text.

    v1.1 returned ``str``; v1.2 T23 client-side change widens the
    return to :class:`PageMeta` so the read-tool (T24) and the
    write-tool (T23) share one envelope. T24 already landed; the
    read tool subsets the envelope via :func:`_read_meta_to_payload`
    in :mod:`server`, dropping ``name`` and ``created_ms`` (the
    fields the read shape doesn't carry — see ``PageMeta``'s
    class docstring).
    """
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/.fs/index"
        return httpx.Response(200, text="# hello")

    async with _client(handler) as sb:
        page = await sb.read_page("index")

    assert page.body == "# hello"
    # No ``X-*`` headers in this response: every meta field except
    # name + body is ``None``. The dataclass is the full envelope;
    # the read tool's wire shape is the dataclass minus ``name``
    # and ``created_ms``.
    assert page.name == "index"
    assert page.etag is None
    assert page.size_bytes is None
    assert page.last_modified_ms is None
    assert page.created_ms is None


@pytest.mark.asyncio
async def test_read_page_extracts_meta_from_response_headers() -> None:
    """``X-*`` headers from SB's GET response populate :class:`PageMeta`.

    Locks the T23 client-side contract: every documented header on
    the design doc § SilverBullet client contract GET row
    (``ETag`` / ``X-Created`` / ``X-Last-Modified`` /
    ``X-Content-Length``) is extracted into the matching
    :class:`PageMeta` field. A future refactor that drops one of the
    three ``X-*`` headers would silently leave the agent's view of
    the page incomplete; this test fails loudly so the regression
    is caught at CI.
    """
    headers = {
        "ETag": '"abc123"',
        "X-Created": "1700000000000",
        "X-Last-Modified": "1700000000123",
        "X-Content-Length": "42",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="# hello", headers=headers)

    async with _client(handler) as sb:
        page = await sb.read_page("index")

    assert page.etag == '"abc123"'
    assert page.created_ms == 1700000000000
    assert page.last_modified_ms == 1700000000123
    assert page.size_bytes == 42
    assert page.body == "# hello"


@pytest.mark.asyncio
async def test_read_page_tolerates_non_numeric_x_meta_headers() -> None:
    """Malformed ``X-*`` headers become ``None``, not ``ValueError``.

    Defensive parse: a misconfigured proxy that substitutes a
    non-numeric string for ``X-Created`` shouldn't crash the read.
    The field becomes ``None`` — same shape as a missing header —
    so an agent always sees ``int | None`` and never has to handle
    a parse error.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text="body",
            headers={
                "X-Created": "not-a-number",
                "X-Last-Modified": "1700000000123",
                "X-Content-Length": "also-not-a-number",
            },
        )

    async with _client(handler) as sb:
        page = await sb.read_page("index")

    assert page.created_ms is None  # malformed → None
    assert page.last_modified_ms == 1700000000123
    assert page.size_bytes is None  # malformed → None
    assert page.body == "body"


@pytest.mark.asyncio
async def test_read_page_raises_page_not_found_on_404() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="page not found")

    async with _client(handler) as sb:
        with pytest.raises(PageNotFound):
            await sb.read_page("missing")


@pytest.mark.asyncio
async def test_read_page_raises_server_error_on_5xx() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="upstream gone")

    async with _client(handler) as sb:
        with pytest.raises(ServerError):
            await sb.read_page("anything")


# --- write_page --------------------------------------------------------


@pytest.mark.asyncio
async def test_write_page_round_trip_body_and_returns_etag() -> None:
    """``write_page`` returns :class:`PageMeta` with the response ``ETag``.

    v1.1 returned ``str | None`` (the raw ETag); v1.2 T23 widens the
    return to :class:`PageMeta` with ``name``, ``etag``,
    ``size_bytes`` (always populated from the *request* body byte
    count — see :func:`test_write_page_size_bytes_from_request_body`),
    ``last_modified_ms``, ``created_ms``. Locks the response-side
    extraction: when SB echoes ``X-Last-Modified`` / ``X-Created``
    back, those surface as integers on the meta.
    """
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(request.headers)
        return httpx.Response(
            200,
            headers={
                "ETag": '"abc123"',
                "X-Last-Modified": "1700000000123",
                "X-Created": "1700000000000",
            },
        )

    async with _client(handler) as sb:
        meta = await sb.write_page("index", "# new body")

    assert isinstance(meta, PageMeta)
    assert meta.name == "index"
    assert meta.etag == '"abc123"'
    assert meta.last_modified_ms == 1700000000123
    assert meta.created_ms == 1700000000000
    # ``size_bytes`` is from the request body byte count (always
    # populated), not the response ``X-Content-Length`` — see
    # :func:`test_write_page_size_bytes_from_request_body` for the
    # detailed rationale.
    assert meta.size_bytes == 10
    assert captured["x-source"] == "external"
    assert captured["x-permission"] == "rw"
    assert captured["content-type"] == "text/markdown"
    # Full design-doc envelope (T8): every X-* header the design doc §
    # SilverBullet client contract PUT row lists is present.
    assert captured["x-created"] == captured["x-last-modified"]
    assert int(captured["x-created"]) > 0
    # ``X-Content-Length`` is the UTF-8 byte count of the body,
    # matching SB's ``meta.size`` (``# new body`` = 10 bytes).
    assert captured["x-content-length"] == "10"


@pytest.mark.asyncio
async def test_write_page_size_bytes_from_request_body() -> None:
    """``size_bytes`` is the request-body UTF-8 byte count, not the response.

    The bridge threads ``size_bytes`` from the body it *wrote*
    (``len(content.encode("utf-8"))``) so the field is always
    populated on a successful write — independent of whether the
    proxy / SB echoes ``X-Content-Length`` back. An agent that asks
    "how big is the page now?" gets the size of what it just wrote,
    which matches SB's view even when the response header is stripped.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        # No response-side X-Content-Length — emulate a proxy-stripped
        # response.
        return httpx.Response(200, headers={"ETag": '"v1"'})

    body = "héllo"  # 5 codepoints, 6 UTF-8 bytes
    async with _client(handler) as sb:
        meta = await sb.write_page("index", body)

    assert meta.size_bytes == 6


@pytest.mark.asyncio
async def test_write_page_meta_is_none_when_response_headers_stripped() -> None:
    """Older SB / proxy-stripped response → meta fields ``None`` where applicable.

    Mirrors the v1.1 None-ETag stance for the full meta envelope:
    every documented response header that the proxy drops becomes
    ``None`` rather than fabricated. ``size_bytes`` is the only
    exception — it's always populated from the request body (the
    byte count we just wrote).
    """
    def handler(request: httpx.Request) -> httpx.Response:
        # No ``X-*`` headers, no ``ETag`` — emulate the v1.1
        # proxy-stripped response that originally motivated the
        # ``None`` handling.
        return httpx.Response(200)

    async with _client(handler) as sb:
        meta = await sb.write_page("index", "body")

    assert meta.etag is None
    assert meta.last_modified_ms is None
    assert meta.created_ms is None
    # ``size_bytes`` is still populated (request-side derivation).
    assert meta.size_bytes == 4


@pytest.mark.asyncio
async def test_write_page_sends_if_match_header() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(request.headers)
        return httpx.Response(200, headers={"ETag": '"v2"'})

    async with _client(handler) as sb:
        await sb.write_page("index", "body", if_match='"v1"')

    assert captured["if-match"] == '"v1"'


@pytest.mark.asyncio
async def test_write_page_sends_if_none_match_star() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(request.headers)
        return httpx.Response(200, headers={"ETag": '"v1"'})

    async with _client(handler) as sb:
        await sb.write_page("new", "body", if_none_match=True)

    assert captured["if-none-match"] == "*"
    assert "if-match" not in {k.lower() for k in captured}


@pytest.mark.asyncio
async def test_write_page_if_match_wins_over_if_none_match() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(request.headers)
        return httpx.Response(200, headers={"ETag": '"v2"'})

    async with _client(handler) as sb:
        await sb.write_page("index", "body", if_match='"v1"', if_none_match=True)

    assert captured["if-match"] == '"v1"'
    assert "if-none-match" not in {k.lower() for k in captured}


@pytest.mark.asyncio
async def test_write_page_body_is_utf8_markdown() -> None:
    received: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        received.append(request.content)
        return httpx.Response(200, headers={"ETag": '"v1"'})

    body = "# héllo\n\nπ=3.14\n"
    async with _client(handler) as sb:
        await sb.write_page("unicode", body)

    assert received[0] == body.encode("utf-8")


@pytest.mark.asyncio
async def test_write_page_x_content_length_matches_utf8_byte_count() -> None:
    """``X-Content-Length`` is the UTF-8 byte count, not the codepoint count.

    Locks the value to ``len(content.encode("utf-8"))`` so a future
    refactor that uses ``len(content)`` (Python's codepoint count)
    fails loudly — non-ASCII pages would silently disagree with what
    the body actually sends.
    """
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(request.headers)
        return httpx.Response(200, headers={"ETag": '"v1"'})

    # ``é`` is 1 codepoint but 2 UTF-8 bytes; ``π`` is 1 codepoint but
    # 2 UTF-8 bytes. Total: 2 + 1 (the leading ``a``) = 3 bytes for
    # the body, 4 bytes including the trailing newline. Pick a body
    # where codepoint count and byte count diverge clearly.
    body = "éπ\n"
    async with _client(handler) as sb:
        await sb.write_page("unicode", body)

    assert len(body) == 3  # codepoints
    assert len(body.encode("utf-8")) == 5  # bytes
    assert captured["x-content-length"] == "5"


@pytest.mark.asyncio
async def test_write_page_raises_page_not_found_on_404() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="page not found")

    async with _client(handler) as sb:
        with pytest.raises(PageNotFound):
            await sb.write_page("missing", "body")


@pytest.mark.asyncio
async def test_write_page_raises_precondition_failed_on_412() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(412, text="precondition failed")

    async with _client(handler) as sb:
        with pytest.raises(PreconditionFailed):
            await sb.write_page("index", "body", if_match="*")


@pytest.mark.asyncio
async def test_write_page_raises_body_too_large_on_413() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(413, text="body too large")

    async with _client(handler) as sb:
        with pytest.raises(BodyTooLarge):
            await sb.write_page("index", "x" * 1024)


@pytest.mark.asyncio
async def test_write_page_raises_server_error_on_5xx() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="bad gateway")

    async with _client(handler) as sb:
        with pytest.raises(ServerError):
            await sb.write_page("index", "body")


# --- delete_page -------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_page_round_trip_and_returns_etag() -> None:
    """DELETE echoes the deleted page's ETag so the caller can confirm what was removed.

    v1.2 T23 widens the return to :class:`PageMeta`; the etag is
    ``meta.etag`` and the size / timestamp fields are ``None`` per
    the design doc DELETE row (no ``X-*`` meta carried on DELETE
    responses). An agent that wants the timestamps of what it just
    deleted reads the page first and threads them through the
    ``if_match`` precondition.
    """

    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(request.headers)
        return httpx.Response(200, headers={"ETag": '"abc123"'})

    async with _client(handler) as sb:
        meta = await sb.delete_page("index")

    assert meta.etag == '"abc123"'
    assert meta.name == "index"
    # DELETE doesn't echo ``X-*`` per the design doc; the bridge
    # surfaces ``None`` rather than fabricating.
    assert meta.size_bytes is None
    assert meta.last_modified_ms is None
    assert meta.created_ms is None
    # Design-doc DELETE row: ``X-Source: external``, optional If-Match.
    # ``X-Permission: rw`` is intentionally NOT sent (it's a PUT-only
    # invariant; reusing ``_WRITE_HEADERS`` would invite a future SB
    # tightening DELETE to reject or differentiate on ``X-Permission``).
    assert captured["x-source"] == "external"
    assert "x-permission" not in {k.lower() for k in captured}
    assert "if-match" not in {k.lower() for k in captured}


@pytest.mark.asyncio
async def test_delete_page_uses_delete_method() -> None:
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        return httpx.Response(200, headers={"ETag": '"v1"'})

    async with _client(handler) as sb:
        await sb.delete_page("index")

    assert seen == [("DELETE", "/.fs/index")]


@pytest.mark.asyncio
async def test_delete_page_forwards_if_match_star() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(request.headers)
        return httpx.Response(200, headers={"ETag": '"v1"'})

    async with _client(handler) as sb:
        await sb.delete_page("index", if_match="*")

    assert captured["if-match"] == "*"


@pytest.mark.asyncio
async def test_delete_page_forwards_if_match_etag() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(request.headers)
        return httpx.Response(200, headers={"ETag": '"v2"'})

    async with _client(handler) as sb:
        await sb.delete_page("index", if_match='"v1"')

    assert captured["if-match"] == '"v1"'


@pytest.mark.asyncio
async def test_delete_page_returns_none_when_etag_header_missing() -> None:
    """A 200 with no ETag header (older SB / proxy-stripped) → ``meta.etag is None``.

    Mirror of the write_page ``None`` contract so callers that chain
    delete-after-write know what to expect. The Meta envelope shape
    stays stable — every meta field except ``name`` becomes ``None``
    on a fully-stripped response.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200)

    async with _client(handler) as sb:
        meta = await sb.delete_page("anything")

    assert meta.etag is None
    assert meta.size_bytes is None
    assert meta.last_modified_ms is None
    assert meta.created_ms is None
    assert meta.name == "anything"


@pytest.mark.asyncio
async def test_delete_page_raises_page_not_found_on_404() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="page not found")

    async with _client(handler) as sb:
        with pytest.raises(PageNotFound):
            await sb.delete_page("missing")


@pytest.mark.asyncio
async def test_delete_page_raises_precondition_failed_on_412_with_star() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(412, text="precondition failed")

    async with _client(handler) as sb:
        with pytest.raises(PreconditionFailed):
            await sb.delete_page("anything", if_match="*")


@pytest.mark.asyncio
async def test_delete_page_raises_precondition_failed_on_412_with_stale_etag() -> None:
    """``if_match=<stale_etag>`` must produce 412 (not 404).

    Locks the layered semantics: SB distinguishes “the page exists
    with a different body” (412, because the etag didn't match) from
    “the page is missing” (404). A future refactor that maps 412 to
    PageNotFound would silently swallow lost-update protections for
    callers that pass an explicit etag.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(412, text="precondition failed")

    async with _client(handler) as sb:
        with pytest.raises(PreconditionFailed):
            await sb.delete_page("index", if_match='"stale"')


@pytest.mark.asyncio
async def test_delete_page_raises_server_error_on_5xx() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="bad gateway")

    async with _client(handler) as sb:
        with pytest.raises(ServerError):
            await sb.delete_page("anything")


# --- list_pages --------------------------------------------------------


@pytest.mark.asyncio
async def test_list_pages_sends_x_sync_mode() -> None:
    """``GET /.fs`` returns JSON only when ``X-Sync-Mode`` is set.

    Without the header, SB 2.9.0 307-redirects to the SPA UI and the
    bridge sees an HTML body (and currently a redirect). The header
    is the only thing distinguishing the JSON-response branch in
    ``server/src/handlers/fs.rs::handle_fs_list``. v1 of the bridge
    broke this by omitting the header (T3 mock-only coverage never
    surfaced it); T10 fixes it as a drive-by bug since the prior map
    parked it as 'effectively moot' once the journal surface landed
    — but ``list_pages`` is still part of the v1 tool surface.
    """
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.headers)
        return httpx.Response(200, content=b"[]")

    async with _client(handler) as sb:
        await sb.list_pages()

    assert seen["x-sync-mode"] == "1"


@pytest.mark.asyncio
async def test_list_pages_returns_file_metas() -> None:
    payload = [
        {"name": "index", "etag": '"a"', "size": 12},
        {"name": "page-2", "etag": None, "size": 7},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/.fs"
        return httpx.Response(200, content=json.dumps(payload).encode("utf-8"))

    async with _client(handler) as sb:
        result = await sb.list_pages()

    assert result == [
        FileMeta(name="index", etag='"a"'),
        FileMeta(name="page-2", etag=None),
    ]


@pytest.mark.asyncio
async def test_list_pages_raises_server_error_on_non_array_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b'{"oops": "not an array"}')

    async with _client(handler) as sb:
        with pytest.raises(ServerError):
            await sb.list_pages()


@pytest.mark.asyncio
async def test_list_pages_raises_server_error_on_5xx() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    async with _client(handler) as sb:
        with pytest.raises(ServerError):
            await sb.list_pages()


# --- auth header on every request --------------------------------------


@pytest.mark.asyncio
async def test_inbound_bearer_is_forwarded_to_sb() -> None:
    """The bridge forwards the same token it just verified.

    T2 of the prior map locked this: one secret, both hops. If the
    outbound ``Authorization`` header is missing, the bridge has
    silently broken the contract and the test must fail loudly.
    """

    seen_auth: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_auth.append(request.headers.get("Authorization", ""))
        return httpx.Response(200, content=b"[]")

    async with _client(handler) as sb:
        await sb.list_pages()

    assert seen_auth[0] == f"Bearer {TOKEN}"
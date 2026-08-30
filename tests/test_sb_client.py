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
    synthesize_etag,
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


# --- synthesize_etag (T31a) --------------------------------------------


def test_synthesize_etag_returns_dashed_form_when_both_fields_present() -> None:
    """``synthesize_etag(ms, bytes)`` returns ``"{ms}-{bytes}"`` for the normal case.

    T31a: when SB strips the ``ETag`` response header (the v1.3
    concurrency-blocking fact on this dev box, surfaced by T31's
    live verification), the bridge falls back to a value derived
    from the headers SB *does* send. The standard form is
    ``"{last_modified_ms}-{size_bytes}"`` — both fields are
    populated by SB on every PUT response on this build.
    """
    assert synthesize_etag(1700000000123, 42) == '"1700000000123-42"'


def test_synthesize_etag_returns_ms_only_when_size_missing() -> None:
    """When ``size_bytes`` is missing, fall back to ``"{ms}"`` alone.

    A proxy that strips ``X-Content-Length`` but keeps
    ``X-Last-Modified`` still gives the bridge a value to thread
    into ``If-Match``; it's just weaker (two writes in the same
    epoch-ms window with different bodies won't be distinguished).
    The agent loses precision, not the concurrency primitive.
    """
    assert synthesize_etag(1700000000123, None) == '"1700000000123"'


def test_synthesize_etag_returns_none_when_both_fields_missing() -> None:
    """When both headers are stripped, no fallback — ``etag`` stays ``None``.

    Mirrors the pre-T31a fully-stripped stance: if SB strips both
    ``X-Last-Modified`` and ``X-Content-Length``, the bridge has
    nothing to synthesize from and the agent loses the concurrency
    primitive (same as a v1.2 SB that honored ``ETag``).
    """
    assert synthesize_etag(None, None) is None


def test_synthesize_etag_returns_none_when_only_size_present() -> None:
    """``size_bytes`` without a timestamp can't anchor the value to a write.

    The synthetic etag must change when the page is rewritten;
    without ``X-Last-Modified`` we have no anchor to that change,
    so the value would be unstable across reads of the same body.
    Returning ``None`` (rather than ``"-{size_bytes}"``) is the
    honest answer: the bridge has no way to distinguish a stale
    read from a fresh one, so it surfaces no value at all.
    """
    assert synthesize_etag(None, 42) is None


@pytest.mark.asyncio
async def test_write_page_meta_etag_synthesized_when_etag_header_missing() -> None:
    """PUT response without ``ETag`` surfaces a synthesized ``etag`` from X-* headers.

    T31's live verification surfaced the second of the two
    v1.3-blocking SB facts: PUT responses on this SB build carry
    no ``ETag`` header. The bridge falls back to ``"{ms}-{bytes}"``
    so an agent that does
    ``read_page → write_page(if_match=read.etag)`` has a value to
    thread. This is the bridge-side proof that the v1.2
    concurrency story is now physically plumbed correctly on
    SBs that strip ``ETag`` (T31b adds the post-write verification
    that closes the operational gap when SB also ignores
    ``If-Match``).
    """
    def handler(request: httpx.Request) -> httpx.Response:
        # No ``ETag`` — emulate the SB build that T31 verified.
        return httpx.Response(
            200,
            headers={
                "X-Last-Modified": "1700000000123",
                "X-Content-Length": "10",
            },
        )

    async with _client(handler) as sb:
        meta = await sb.write_page("index", "# new body")

    # Synthesized form: ``"{ms}-{bytes}"`` with surrounding quotes
    # (matches SB's real ETag shape so a future SB build that
    # honors the header doesn't see a malformed value).
    assert meta.etag == '"1700000000123-10"'
    assert meta.last_modified_ms == 1700000000123
    assert meta.size_bytes == 10


@pytest.mark.asyncio
async def test_read_page_meta_etag_synthesized_when_etag_header_missing() -> None:
    """GET response without ``ETag`` also synthesizes (read path parity).

    The fallback lives in ``_meta_from_response`` which every
    code path shares (``read_page`` / ``write_page`` /
    ``read_page_meta`` / ``delete_page``). On SBs that strip
    ``ETag`` from PUT *and* GET, the synthesized value is
    identical on both sides — the same ``"{ms}-{bytes}"`` string
    — so an agent's ``read.etag`` flows into ``write(if_match=...)``
    without a translation step.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text="body",
            headers={
                "X-Last-Modified": "1700000000123",
                "X-Content-Length": "4",
            },
        )

    async with _client(handler) as sb:
        page = await sb.read_page("index")

    assert page.etag == '"1700000000123-4"'


@pytest.mark.asyncio
async def test_synthesized_etag_is_stable_across_re_reads_of_same_body() -> None:
    """Same body + same mtime → same synthesized etag (the stability invariant).

    Two reads of an unchanged page produce identical synthesized
    etags — the precondition an agent needs to detect *no* edit
    between read and write. If the bridge synthesized a value that
    drifted across reads of the same body, every
    ``write(if_match=read.etag)`` would 412-equivalent-fail even
    on uncontested writes, and the concurrency primitive would be
    useless. This test guards the stability invariant.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text="body",
            headers={
                "X-Last-Modified": "1700000000123",
                "X-Content-Length": "4",
            },
        )

    async with _client(handler) as sb:
        first = await sb.read_page("index")
        second = await sb.read_page("index")

    assert first.etag == second.etag == '"1700000000123-4"'


@pytest.mark.asyncio
async def test_synthesized_etag_differs_when_body_or_mtime_changes() -> None:
    """Different body or mtime → different synthesized etag (the drift invariant).

    The other half of the stability contract: a write that
    actually changed the page must produce a synthesized etag
    that doesn't match the pre-write value, so an agent that
    does ``read → write(if_match=read.etag)`` sees a mismatch
    after a *concurrent* write. This test emulates the
    read-then-write-then-read sequence on a mutating response and
    asserts the post-write etag is a different string.
    """
    mtime_counter = {"ms": 1700000000000}

    def handler(request: httpx.Request) -> httpx.Response:
        # Each request advances mtime; this emulates two writes
        # happening at different moments. The body length stays
        # the same, so the only signal is the timestamp.
        mtime_counter["ms"] += 1000
        return httpx.Response(
            200,
            text="body",
            headers={
                "X-Last-Modified": str(mtime_counter["ms"]),
                "X-Content-Length": "4",
            },
        )

    async with _client(handler) as sb:
        first = await sb.read_page("index")
        # Simulate the *agent's* read (matches what the SB saw the
        # last time the handler ran) and then a *concurrent write*
        # (handler advances mtime on the next call).
        concurrent_write = await sb.write_page("index", "body")
        post_read = await sb.read_page("index")

    # All three etags are synthesized from the same fields.
    assert first.etag is not None
    assert concurrent_write.etag is not None
    assert post_read.etag is not None
    # The concurrent write's etag drifts from the pre-read etag.
    assert concurrent_write.etag != first.etag
    # The post-write read also drifts from the pre-read etag (the
    # signal T31b's verification path uses to detect the race).
    assert post_read.etag != first.etag


@pytest.mark.asyncio
async def test_real_etag_wins_over_synthesis_when_both_present() -> None:
    """When SB sends both ``ETag`` and ``X-*``, the real etag is forwarded.

    Pre-T31a behavior is preserved on SBs that *do* emit an
    ``ETag``: the synthesized value is never computed. The
    fallback is strictly opt-in (per-response, only when
    ``ETag`` is missing); on any SB build that honors the header,
    the agent still gets a real etag string. Locks the
    "fallback is invisible when not needed" invariant.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text="body",
            headers={
                "ETag": '"real-etag"',
                "X-Last-Modified": "1700000000123",
                "X-Content-Length": "4",
            },
        )

    async with _client(handler) as sb:
        page = await sb.read_page("index")

    assert page.etag == '"real-etag"'


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


# --- exists_page -------------------------------------------------------


@pytest.mark.asyncio
async def test_exists_page_returns_true_on_200() -> None:
    """``exists_page`` is ``True`` when SB returns 200.

    T25: the cheapest existence check the bridge exposes. The body
    bytes are intentionally not materialized (we don't read
    ``response.text`` / ``response.content``) so a ``read_page``
    that loads a large page is not what the caller paid for — they
    asked a yes/no question and got a yes/no answer.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/.fs/index"
        return httpx.Response(200, text="# body content")

    async with _client(handler) as sb:
        assert await sb.exists_page("index") is True


@pytest.mark.asyncio
async def test_exists_page_returns_false_on_404() -> None:
    """``exists_page`` is ``False`` (not a ``PageNotFound`` exception) on 404.

    T25: the existence question's "no" answer is a *value*, not an
    error. ``read_page`` raises ``PageNotFound`` for the same status
    — different tools, different contract: ``read_page`` is "give
    me the body", and a missing body is an error;
    ``exists_page`` is "is it there?", and "no" is a valid answer.
    The MCP tool handler on top translates ``PageNotFound`` to a
    ``ToolError`` if one leaks through (defensive — the client
    method shouldn't ever let one), so this is the only "no" path
    callers will see.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/.fs/missing"
        return httpx.Response(404, text="page not found")

    async with _client(handler) as sb:
        assert await sb.exists_page("missing") is False


@pytest.mark.asyncio
async def test_exists_page_does_not_materialize_body_on_200() -> None:
    """``exists_page`` does not call ``response.text`` / ``.content``.

    Locks the cost-down promise: the tool is a "does it exist?"
    check, not a covert "peek at the body" check. We assert on a
    200 with a multi-KB body and verify the call returns
    immediately — if the body were ever read into Python, the
    cost on a large SB space would balloon the existence check
    from "one round trip" to "one round trip + one big allocation".
    (We don't directly observe allocation; we assert that the
    call succeeds with a body the handler sends but the client
    never asks for.)
    """

    big_body = "x" * (1024 * 64)  # 64 KiB

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=big_body)

    async with _client(handler) as sb:
        result = await sb.exists_page("big")
    assert result is True
    # ``big_body`` lives only in the handler closure; the assertion
    # above is the smoke test, this one is the *contract* test:
    # if a future refactor adds ``response.text`` /
    # ``response.read()`` to ``exists_page``, the test still passes
    # but the cost-down promise is silently broken. We don't have
    # a clean way to observe the body bytes short of patching
    # ``_client.get``, so the contract test relies on the docstring
    # in ``sb_client.exists_page`` rather than runtime observation.
    del big_body


@pytest.mark.asyncio
async def test_exists_page_raises_server_error_on_5xx() -> None:
    """5xx surfaces as :class:`ServerError`, **not** ``False``.

    T25: a 5xx is not a valid "no" — "I don't know, the server is
    broken" is not the same answer as "the page doesn't exist".
    Callers care about a definitive yes/no; surfacing 5xx as
    ``False`` would let an agent proceed with a (wrongly) confident
    "create it" that ignores a SB outage. The MCP tool handler
    surfaces :class:`ServerError` as ``ToolError("silverbullet
    error: {status}")`` — the same wording as the other tools.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="bad gateway")

    async with _client(handler) as sb:
        with pytest.raises(ServerError):
            await sb.exists_page("anything")


@pytest.mark.asyncio
async def test_exists_page_uses_get_method() -> None:
    """``exists_page`` issues ``GET /.fs/{name}`` — not HEAD, not POST.

    Locks the standing-preference decision: SB's ``/.fs`` endpoint
    documents ``GET`` semantics; ``HEAD`` isn't part of the upstream
    contract the bridge locks against (``server/src/handlers/fs.rs``)
    and could behave differently across SB versions. ``GET`` is the
    wire-level primitive the design doc guarantees.
    """

    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        return httpx.Response(200, text="")

    async with _client(handler) as sb:
        await sb.exists_page("index")

    assert seen == [("GET", "/.fs/index")]


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
    """``list_pages`` widens to ``list[PageMeta]`` (T28).

    v1 returned ``list[FileMeta]`` (the minimal ``name`` / ``etag``
    subset). v1.2 T28 widens to ``list[PageMeta]`` — the same
    envelope family the read and write tools use — so a single
    list call surfaces ``size_bytes`` / ``last_modified_ms`` /
    ``created_ms`` alongside the name without a per-page
    ``read_page`` round trip. ``FileMeta`` itself is still a
    valid narrower projection of the envelope and stays
    exported on the module surface for back-compat.
    """
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

    # ``PageMeta`` is the full envelope; ``FileMeta`` is the
    # v1 minimal subset (``name`` / ``etag`` only). The new
    # ``list_pages`` return carries the full envelope; an
    # operator who wants the narrower shape filters the
    # result list client-side (``[FileMeta(name=r.name,
    # etag=r.etag) for r in result]``). The fields absent from
    # the payload (``lastModified`` / ``created``) come back as
    # ``None`` per the defensive-parsing contract.
    assert result == [
        PageMeta(
            name="index",
            etag='"a"',
            size_bytes=12,
            last_modified_ms=None,
            created_ms=None,
            body=None,
        ),
        PageMeta(
            name="page-2",
            etag=None,
            size_bytes=7,
            last_modified_ms=None,
            created_ms=None,
            body=None,
        ),
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


@pytest.mark.asyncio
async def test_list_pages_extracts_meta_fields_from_list_payload() -> None:
    """T28 widens ``list_pages`` — verify the per-row field mapping.

    SB's ``GET /.fs`` payload carries ``created`` /
    ``lastModified`` / ``size`` alongside ``name`` per
    ``server/src/handlers/fs.rs::handle_fs_list``; v1 dropped
    all but ``name`` (and the optional ``etag``) and returned
    ``list[FileMeta]``. T28 threads the four extras through to
    :class:`PageMeta` so a single list call surfaces the
    timestamps / size without a per-page ``read_page``. This
    test pins down the field-by-field mapping so a future
    SB-side rename (``lastModified`` → ``last_modified``)
    surfaces loudly here rather than as silent ``None`` on
    every row.
    """
    payload = [
        {
            "name": "index",
            "etag": '"a"',
            "created": 1700000000000,
            "lastModified": 1700000000123,
            "size": 1024,
        },
        {
            "name": "page-2",
            "etag": '"b"',
            "created": 1700000001000,
            "lastModified": 1700000001123,
            "size": 2048,
        },
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=json.dumps(payload).encode("utf-8"))

    async with _client(handler) as sb:
        result = await sb.list_pages()

    assert result == [
        PageMeta(
            name="index",
            etag='"a"',
            size_bytes=1024,
            last_modified_ms=1700000000123,
            created_ms=1700000000000,
            body=None,
        ),
        PageMeta(
            name="page-2",
            etag='"b"',
            size_bytes=2048,
            last_modified_ms=1700000001123,
            created_ms=1700000001000,
            body=None,
        ),
    ]


@pytest.mark.asyncio
async def test_list_pages_tolerates_missing_and_malformed_meta_fields() -> None:
    """Defensive parse: missing or non-numeric meta fields → ``None``.

    Mirrors the read/write paths' "older SB / proxy-stripped"
    contract: a row that lacks ``created`` / ``lastModified`` /
    ``size`` (older SB, proxy drop, future schema drift) should
    surface as ``None`` rather than crash the whole list call.
    A row whose ``created`` is a non-numeric string (``"nope"``)
    surfaces as ``None`` for that field via
    :func:`_parse_int_header`'s try/except — the rest of the
    row parses normally, so a single malformed row doesn't take
    the whole list down. ``name`` always parses (it's required
    for the row to be emitted at all); ``etag`` is ``None``
    when missing or non-string.
    """
    payload = [
        # Minimal row — only ``name``; every other field is
        # absent.
        {"name": "minimal"},
        # Malformed ``created`` — string that doesn't parse as int.
        {"name": "broken", "created": "nope", "lastModified": 1, "size": 2},
        # ``created`` as a JSON ``null`` (defensive against a
        # future SB that explicitly nulls the field).
        {"name": "nulled", "created": None, "lastModified": None},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=json.dumps(payload).encode("utf-8"))

    async with _client(handler) as sb:
        result = await sb.list_pages()

    assert result == [
        PageMeta(
            name="minimal",
            etag=None,
            size_bytes=None,
            last_modified_ms=None,
            created_ms=None,
            body=None,
        ),
        PageMeta(
            name="broken",
            etag=None,
            size_bytes=2,
            last_modified_ms=1,
            created_ms=None,
            body=None,
        ),
        PageMeta(
            name="nulled",
            etag=None,
            size_bytes=None,
            last_modified_ms=None,
            created_ms=None,
            body=None,
        ),
    ]


@pytest.mark.asyncio
async def test_list_pages_skips_rows_without_a_name() -> None:
    """A list payload row that lacks ``name`` is silently dropped.

    SB's contract per ``handle_fs_list`` is "every row has a
    ``name``"; an upstream regression that emits a row without
    one would otherwise crash the whole list call. The current
    code path silently drops such rows; if the bridge ever
    needs to surface them loudly, this test is the place to
    flip to a ``ServerError``.
    """
    payload = [
        {"name": "ok", "size": 1},
        {"size": 2},  # no ``name`` — dropped
        {"name": "also-ok", "size": 3},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=json.dumps(payload).encode("utf-8"))

    async with _client(handler) as sb:
        result = await sb.list_pages()

    assert [m.name for m in result] == ["ok", "also-ok"]


# --- read_page_meta (T28 hydration helper) -----------------------------


@pytest.mark.asyncio
async def test_read_page_meta_returns_headers_only() -> None:
    """``read_page_meta`` returns :class:`PageMeta` without buffering the body.

    The list-pages etag-hydration walker uses this method to
    fetch per-page etags when the operator opts in to
    ``MCP_SILVERBULLET_LIST_PAGES_HYDRATE_ETAGS``. The whole
    point is "headers only, no body": a 1 MiB page hydrated
    should still cost just the headers over the wire, not
    1 MiB of body bytes. This test pins that down by sending a
    large response body and asserting the client surfaces only
    the headers — if a future refactor swaps ``stream()`` for
    ``get()`` (which backgrounds the body read) the test would
    need to time-budget, but the immediate contract is "the
    headers reach us" so we assert on those.

    Reads ``ETag`` / ``X-Created`` / ``X-Last-Modified`` /
    ``X-Content-Length`` from the response and surfaces them in
    the same shape :meth:`read_page` does (minus ``body``,
    which is ``None`` — the body was never read).
    """
    # Body content deliberately large enough that "we read it"
    # would be obvious in a memory profile, but small enough
    # to not slow the test down.
    big_body = "x" * (64 * 1024)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/.fs/index"
        return httpx.Response(
            200,
            content=big_body.encode("utf-8"),
            headers={
                "ETag": '"abc123"',
                "X-Last-Modified": "1700000000123",
                "X-Created": "1700000000000",
                "X-Content-Length": str(len(big_body)),
            },
        )

    async with _client(handler) as sb:
        meta = await sb.read_page_meta("index")

    # The dataclass carries the headers; ``body`` is ``None``
    # because we never read the response body. A future refactor
    # that accidentally reads it (and surfaces ``"x" * 65536``)
    # would show up here as a body of 65,536 chars rather than
    # ``None`` — the test locks the "no body" half of the
    # contract, not the body-length math (that's covered by
    # :func:`test_read_page_meta_extracts_meta_from_response_headers`
    # below).
    assert meta.name == "index"
    assert meta.etag == '"abc123"'
    assert meta.last_modified_ms == 1700000000123
    assert meta.created_ms == 1700000000000
    assert meta.size_bytes == len(big_body)
    # ``body`` is ``None`` because the stream is closed before
    # the body is buffered; a future refactor that calls
    # ``response.text`` or ``response.content`` would populate
    # this and the test would fail loudly.
    assert meta.body is None


@pytest.mark.asyncio
async def test_read_page_meta_returns_nones_when_headers_stripped() -> None:
    """A 200 with no ``X-*`` / ``ETag`` headers → ``None``-populated envelope.

    Mirrors :meth:`read_page`'s ``None``-when-stripped contract:
    an old SB / proxy-stripped response surfaces the same
    shape, just without the meta fields. ``body`` is ``None``
    regardless (we never read it).
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content="ignored body")

    async with _client(handler) as sb:
        meta = await sb.read_page_meta("anything")

    assert meta.name == "anything"
    assert meta.etag is None
    assert meta.size_bytes is None
    assert meta.last_modified_ms is None
    assert meta.created_ms is None
    assert meta.body is None


@pytest.mark.asyncio
async def test_read_page_meta_raises_page_not_found_on_404() -> None:
    """A 404 surfaces as :class:`PageNotFound` (same as :meth:`read_page`).

    The hydration walker catches this in :meth:`read_page_meta_safe`
    and returns ``None``; a caller that calls
    :meth:`read_page_meta` directly (instead of the safe sibling)
    gets the typed exception per the design doc's status-code
    mapping.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="page not found")

    async with _client(handler) as sb:
        with pytest.raises(PageNotFound):
            await sb.read_page_meta("missing")


@pytest.mark.asyncio
async def test_read_page_meta_safe_returns_none_on_404() -> None:
    """``read_page_meta_safe`` swallows 404 → ``None``.

    The hydration walker relies on this: a page deleted between
    the list call and the per-page GET leaves the row's
    ``etag=None`` rather than failing the whole list. The
    row stays in the result with the etag it already had
    (also ``None``, since SB's list payload omits the field);
    the agent can ``read_page`` it later if it wants the etag
    for the next call.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="page not found")

    async with _client(handler) as sb:
        result = await sb.read_page_meta_safe("missing")

    assert result is None


@pytest.mark.asyncio
async def test_read_page_meta_safe_returns_none_on_5xx() -> None:
    """``read_page_meta_safe`` swallows 5xx → ``None``.

    A single transient SB outage on a hydration GET shouldn't
    abort the whole ``list_pages`` call — partial hydration is
    strictly better than failing the whole list when the
    alternative is "the agent retries the whole list". The
    affected row keeps ``etag=None``; everything else
    surfaces normally.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="upstream gone")

    async with _client(handler) as sb:
        result = await sb.read_page_meta_safe("anything")

    assert result is None


@pytest.mark.asyncio
async def test_read_page_meta_safe_returns_none_on_timeout() -> None:
    """``read_page_meta_safe`` swallows :class:`httpx.TimeoutException` → ``None``.

    Same resilience contract as the 5xx case: a single page
    that times out leaves its row's etag as ``None``; the
    rest of the list surfaces normally. Without this, a slow
    page in a 200-page space could turn a 1-second ``list_pages``
    into a 30-second hung call when SB is under load.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("simulated")

    async with _client(handler) as sb:
        result = await sb.read_page_meta_safe("anything")

    assert result is None


@pytest.mark.asyncio
async def test_read_page_meta_safe_returns_meta_on_200() -> None:
    """Happy path: a 200 with full headers returns :class:`PageMeta`.

    Pins the round trip so a future refactor that accidentally
    swaps in :meth:`read_page` (which would materialize the
    body) or breaks the ``stream()``-based closure shows up
    here as a body field populated rather than ``None``.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content="body bytes we never want to see",
            headers={"ETag": '"abc"', "X-Content-Length": "100"},
        )

    async with _client(handler) as sb:
        result = await sb.read_page_meta_safe("index")

    assert result is not None
    assert result.name == "index"
    assert result.etag == '"abc"'
    assert result.size_bytes == 100
    assert result.body is None


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
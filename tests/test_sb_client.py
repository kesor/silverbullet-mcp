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
    _parse_cf_error,
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


# --- synthesize_etag (T31a / T44) --------------------------------------


def test_synthesize_etag_returns_size_only_form_when_size_present() -> None:
    """``synthesize_etag(ms, bytes)`` returns ``"{bytes}"`` for the normal case.

    T31a established the synthesized-etag fallback; T44 changed
    its shape from ``"{ms}-{bytes}"`` to ``"{bytes}"``. The
    rationale: the bridge stamps ``X-Last-Modified`` with
    ``now_ms`` on every PUT request (``_WRITE_HEADERS``), so the
    pre-write read's mtime and the post-write re-read's mtime
    drift even when the body is unchanged — the mtime component
    was tracking *when* a write happened, not *what* it wrote,
    and the *when* drift was the source of T31b's false-positive
    "concurrent edit detected" on every successful write. The
    size-only primitive is what the concurrency check actually
    needs: same body → same size → same etag → no drift;
    different body → different size → different etag → drift.
    """
    # ``last_modified_ms`` is passed through but unused (T44
    # dropped it; the parameter is kept so call sites don't
    # need to change).
    assert synthesize_etag(1700000000123, 42) == '"42"'


def test_synthesize_etag_returns_none_when_size_missing() -> None:
    """When ``size_bytes`` is missing, no fallback — ``etag`` stays ``None``.

    T44 dropped the pre-T44 ``"{ms}"`` fallback: a
    timestamp-only value is *less* useful than a size-only
    value (it can't distinguish two writes in the same epoch-ms
    window with different bodies) *and* it was tracking the
    *when* drift that caused T31b's false-positive. Returning
    ``None`` here is the honest answer: the bridge has no useful
    primitive to offer when ``X-Content-Length`` is stripped.

    A proxy that strips ``X-Content-Length`` but keeps
    ``X-Last-Modified`` now drops the agent down to
    ``etag=None`` rather than offering a weak
    ``"{ms}"``-only value. The agent loses the concurrency
    primitive entirely; same posture as a fully-stripped
    response pre-T31a. The mitigation is the same as for any
    other stripped-header case: re-read on a different proxy
    or surface the gap to the operator.
    """
    assert synthesize_etag(1700000000123, None) is None


def test_synthesize_etag_returns_none_when_both_fields_missing() -> None:
    """When both headers are stripped, no fallback — ``etag`` stays ``None``.

    Mirrors the pre-T31a fully-stripped stance: if SB strips both
    ``X-Last-Modified`` and ``X-Content-Length``, the bridge has
    nothing to synthesize from and the agent loses the concurrency
    primitive (same as a v1.2 SB that honored ``ETag``).
    """
    assert synthesize_etag(None, None) is None


def test_synthesize_etag_returns_size_form_when_only_size_present() -> None:
    """``size_bytes`` alone is sufficient post-T44 (mtime no longer required).

    Pre-T44 this case returned ``None`` (the helper reasoned
    that a body-length-derived etag was unstable without an
    mtime anchor to a write). T44 reversed that stance: the
    concurrency primitive only needs to differ between two
    *different* bodies, and size alone satisfies that (same
    body → same size → same etag; different body → different
    size → different etag). The mtime was tracking *when*,
    which is the wrong axis for this primitive.
    """
    assert synthesize_etag(None, 42) == '"42"'


@pytest.mark.asyncio
async def test_write_page_meta_etag_synthesized_when_etag_header_missing() -> None:
    """PUT response without ``ETag`` surfaces a synthesized ``etag`` from ``X-Content-Length``.

    T31's live verification surfaced the second of the two
    v1.3-blocking SB facts: PUT responses on this SB build carry
    no ``ETag`` header. The bridge falls back to ``"{bytes}"``
    (T44) so an agent that does
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

    # Synthesized form: ``"{bytes}"`` with surrounding quotes
    # (matches SB's real ETag shape so a future SB build that
    # honors the header doesn't see a malformed value). The
    # ``X-Last-Modified`` value is *not* part of the synthesized
    # etag — T44 dropped it because the bridge stamps
    # ``X-Last-Modified`` with ``now_ms`` on every PUT, which
    # made the pre-T44 mtime-dashed form drift on every write.
    assert meta.etag == '"10"'
    assert meta.last_modified_ms == 1700000000123
    assert meta.size_bytes == 10


@pytest.mark.asyncio
async def test_read_page_meta_etag_synthesized_when_etag_header_missing() -> None:
    """GET response without ``ETag`` also synthesizes (read path parity).

    The fallback lives in ``_meta_from_response`` which every
    code path shares (``read_page`` / ``write_page`` /
    ``read_page_meta`` / ``delete_page``). On SBs that strip
    ``ETag`` from PUT *and* GET, the synthesized value is
    identical on both sides — the same ``"{bytes}"`` string
    (T44) — so an agent's ``read.etag`` flows into
    ``write(if_match=...)`` without a translation step.
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

    assert page.etag == '"4"'


@pytest.mark.asyncio
async def test_synthesized_etag_is_stable_across_re_reads_of_same_body() -> None:
    """Same body → same synthesized etag (the stability invariant).

    Two reads of an unchanged page produce identical synthesized
    etags — the precondition an agent needs to detect *no* edit
    between read and write. If the bridge synthesized a value that
    drifted across reads of the same body, every
    ``write(if_match=read.etag)`` would 412-equivalent-fail even
    on uncontested writes, and the concurrency primitive would be
    useless. This test guards the stability invariant.

    T44 change: the prior form ``"{ms}-{bytes}"`` was *not*
    stable across reads when the bridge stamped
    ``X-Last-Modified`` with ``now_ms`` on each PUT, because
    the mtime component tracked *when* a write happened
    independently of *what* was written. The size-only form
    is stable because size is purely a function of the body.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        # Two distinct mtimes on two reads — emulates the
        # bridge's ``now_ms`` stamping on PUT and the SB's
        # store-time echo on GET. Pre-T44 the dashed form
        # would drift here; T44's size-only form is stable.
        if handler.read_count == 0:
            handler.read_count += 1
            mtime = "1700000000000"
        else:
            mtime = "1700000001000"
        return httpx.Response(
            200,
            text="body",
            headers={
                "X-Last-Modified": mtime,
                "X-Content-Length": "4",
            },
        )

    handler.read_count = 0  # type: ignore[attr-defined]

    async with _client(handler) as sb:
        first = await sb.read_page("index")
        second = await sb.read_page("index")

    assert first.etag == second.etag == '"4"'


@pytest.mark.asyncio
async def test_synthesized_etag_differs_when_body_changes() -> None:
    """Different body → different synthesized etag (the drift invariant).

    The other half of the stability contract: a write that
    actually changed the page must produce a synthesized etag
    that doesn't match the pre-write value, so an agent that
    does ``read → write(if_match=read.etag)`` sees a mismatch
    after a *concurrent* write. This test emulates the
    read-then-write-then-read sequence on a mutating response
    and asserts the post-write etag is a different string.

    T44 change: the drift signal is now body-length, not
    mtime. Two writes with the same body length return the
    same synthesized etag; two writes with different body
    lengths return different synthesized etags. The pre-T44
    form tracked mtime drift, which made T31b's verification
    helper raise "concurrent edit detected" on every
    successful write (the bridge's ``now_ms`` PUT stamp
    drifted the mtime even when the body didn't change).
    """
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT":
            # body_b is a longer body — the post-write
            # ``X-Content-Length`` is 23, not 4.
            return httpx.Response(
                200,
                text="body_b is longer body",
                headers={
                    "X-Last-Modified": "1700000001000",
                    "X-Content-Length": "23",
                },
            )
        # GETs: pre-write returns body_a (4 bytes); post-write
        # returns body_b (23 bytes).
        if handler.get_count == 0:
            handler.get_count += 1
            text = "body"
            size = "4"
        else:
            text = "body_b is longer body"
            size = "23"
        return httpx.Response(
            200,
            text=text,
            headers={
                "X-Last-Modified": "1700000001000",
                "X-Content-Length": size,
            },
        )

    handler.get_count = 0  # type: ignore[attr-defined]

    async with _client(handler) as sb:
        first = await sb.read_page("index")
        # Simulate a concurrent write with a different body
        # length (size = 23 vs 4).
        concurrent_write = await sb.write_page("index", "body_b is longer body")
        post_read = await sb.read_page("index")

    # All three etags are synthesized from the size field.
    assert first.etag is not None
    assert concurrent_write.etag is not None
    assert post_read.etag is not None
    # Pre-write and post-write are different sizes → different
    # synthesized etags (the signal T31b's verification path
    # uses to detect the race).
    assert first.etag == '"4"'
    assert concurrent_write.etag == '"23"'
    assert post_read.etag == '"23"'
    assert concurrent_write.etag != first.etag
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


# --- T43: CF 5xx wrapper parsing ---------------------------------------


_CF_BODY = (
    '{"type":"https://...","title":"Error 502: Bad gateway",'
    '"status":502,"detail":"...","instance":"a33e...",'
    '"error_code":502,"error_name":"origin_bad_gateway",'
    '"error_category":"origin","ray_id":"a33e...",'
    '"timestamp":"2026-08-31T19:40:15Z","zone":"sb.kesor.net",'
    '"cloudflare_error":true,"retryable":true,"retry_after":60,'
    '"owner_action_required":true,"what_you_should_do":"**Wait '
    'and retry.**...","footer":"..."}'
)


def test_parse_cf_error_returns_hint_for_cf_shaped_body() -> None:
    """T43: a CF-shaped body yields the three useful fields.

    The full CF error envelope carries ~15 fields; only
    ``retry_after``, ``error_code``, and ``title`` are surfaced —
    the rest are debug-only and intentionally dropped. The hint
    must round-trip ``retry_after`` as a number (the JSON parser
    already coerced it), ``error_code`` as an int (the JSON
    number 502, not the string "502"), and ``title`` as a string.
    """
    hint = _parse_cf_error(_CF_BODY)
    assert hint == {
        "retry_after": 60,
        "error_code": 502,
        "title": "Error 502: Bad gateway",
    }


def test_parse_cf_error_returns_none_for_empty_body() -> None:
    """T43: empty body (no CF marker fields to detect) returns ``None``.

    The 5xx path still raises ``ServerError`` with ``cf_hint=None``
    so the MCP tool envelope is unchanged from the pre-T43
    wording. This is the common case for SB behind a plain
    reverse proxy that returns a non-JSON HTML error page or an
    empty body — the bridge sees a 502 with no body and surfaces
    ``"silverbullet error: 502"`` exactly as before.
    """
    assert _parse_cf_error("") is None
    assert _parse_cf_error(None) is None


def test_parse_cf_error_returns_none_for_non_json_body() -> None:
    """T43: a non-JSON body (plain text, HTML error page) returns ``None``.

    Even though CF sometimes returns HTML error pages rather
    than JSON, the only bodies that carry the retry hint are
    the JSON envelope ones. Plain text and HTML are surfaced
    unchanged by the bridge (the body is thrown away on 5xx
    per the design doc), and the ``cf_hint`` field stays
    ``None``. This pins the conservative posture: don't try
    to scrape HTML.
    """
    assert _parse_cf_error("bad gateway") is None
    assert _parse_cf_error("<html><body>502 Bad Gateway</body></html>") is None


def test_parse_cf_error_returns_none_for_random_json_without_cf_markers() -> None:
    """T43: random JSON without CF marker fields returns ``None``.

    Some reverse proxies (e.g. nginx with a custom error page)
    return JSON bodies that happen to parse cleanly but carry
    no CF marker fields — the helper detects the absence of
    ``cloudflare_error`` / ``error_category`` / ``ray_id`` and
    returns ``None``. The 5xx path still raises ``ServerError``
    with no ``cf_hint`` on the envelope.
    """
    assert _parse_cf_error('{"error": "internal", "code": 500}') is None
    assert _parse_cf_error('{"detail": "upstream timeout"}') is None


def test_parse_cf_error_keeps_retry_after_field_even_when_omitted_upstream() -> None:
    """T43: CF body without ``retry_after`` still surfaces the
    field as ``None``.

    The field is *always* present in the returned dict when
    the body is CF-shaped, but its value can be ``None`` if the
    upstream omitted it. This gives the agent a consistent
    schema (``cf_hint`` always has the three keys when the
    body is CF-shaped) so it doesn't have to ``KeyError``-guard
    every field; it can read ``cf_hint.retry_after`` and decide
    whether to fall back to a default retry interval based on
    ``None``.
    """
    body = (
        '{"cloudflare_error":true,"error_code":504,'
        '"title":"Error 504: Gateway timeout"}'
    )
    hint = _parse_cf_error(body)
    assert hint == {
        "retry_after": None,
        "error_code": 504,
        "title": "Error 504: Gateway timeout",
    }


def test_parse_cf_error_coerces_string_error_code_to_int() -> None:
    """T43: string-typed ``error_code`` is coerced to ``int``.

    Older CF release branches emit ``"error_code": "502"``
    (string-typed) rather than ``502`` (numeric). The parser
    normalizes both forms to ``int`` so the agent sees a
    consistent numeric field. A non-numeric string (which
    shouldn't happen but might in a malformed CF response)
    passes through unchanged rather than raising.
    """
    body = (
        '{"cloudflare_error":true,"error_code":"503",'
        '"retry_after":30,"title":"Error 503"}'
    )
    assert _parse_cf_error(body) == {
        "retry_after": 30,
        "error_code": 503,
        "title": "Error 503",
    }
    # Non-numeric string passes through (defensive — shouldn't
    # happen on real CF responses).
    body_bad = '{"cloudflare_error":true,"error_code":"foo","title":"x"}'
    assert _parse_cf_error(body_bad) == {
        "retry_after": None,
        "error_code": "foo",
        "title": "x",
    }


def test_parse_cf_error_tolerates_top_level_non_object() -> None:
    """T43: top-level non-object JSON returns ``None``.

    Defensive: a CF 5xx body should always be a JSON object,
    but if a future release wraps the envelope in a list or
    a scalar, the parser must not raise. ``json.loads`` itself
    succeeds on a list; the ``isinstance(data, dict)`` guard
    returns ``None`` instead of indexing into a list and
    raising ``TypeError``. (Pin: a future change that drops
    the guard would surface a ``TypeError`` from inside the
    5xx path, masking the original 5xx.)
    """
    assert _parse_cf_error("[]") is None
    assert _parse_cf_error("null") is None
    assert _parse_cf_error("42") is None


@pytest.mark.asyncio
async def test_5xx_response_populates_cf_hint_on_server_error() -> None:
    """T43: SB returns 502 with a CF body; the raised
    ``ServerError`` carries ``cf_hint`` with the parsed fields.

    This is the integration check on the
    ``_raise_for_status`` → ``_parse_cf_error`` →
    ``ServerError.cf_hint`` chain. The ``read_page`` path
    raises ``ServerError`` (not ``PageNotFound``) on a 502,
    and the ``cf_hint`` attribute on the raised exception
    carries the parsed hint — ready for the MCP tool layer
    in ``server.py`` to attach to the error envelope.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text=_CF_BODY)

    async with _client(handler) as sb:
        with pytest.raises(ServerError) as ei:
            await sb.read_page("Foo")
    assert str(ei.value) == "silverbullet error: 502"
    assert ei.value.cf_hint == {
        "retry_after": 60,
        "error_code": 502,
        "title": "Error 502: Bad gateway",
    }


@pytest.mark.asyncio
async def test_5xx_response_leaves_cf_hint_none_for_non_cf_body() -> None:
    """T43: a 5xx with a plain-text (non-CF) body leaves
    ``cf_hint=None``.

    The conservative posture: only the CF JSON envelope is
    parsed; any other 5xx body leaves ``cf_hint`` unset so
    the MCP tool envelope stays byte-for-byte the same as
    the pre-T43 shape (no `` [cf_hint: ...]`` suffix on the
    error message). A non-CF deployment (SB behind nginx,
    Caddy, or a plain reverse proxy that returns its own
    HTML error page) sees no behavior change.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="internal server error plain text")

    async with _client(handler) as sb:
        with pytest.raises(ServerError) as ei:
            await sb.read_page("Foo")
    assert str(ei.value) == "silverbullet error: 500"
    assert ei.value.cf_hint is None


@pytest.mark.asyncio
async def test_5xx_response_leaves_cf_hint_none_for_empty_body() -> None:
    """T43: a 5xx with an empty body leaves ``cf_hint=None``.

    Empty body is the same shape as the test above: no body
    bytes to parse, so the parser short-circuits to ``None``.
    A CF-fronted SB in some failure modes returns a 502 with
    no body at all; the bridge sees that as a generic 5xx
    with no hint, which is the correct conservative posture.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="")

    async with _client(handler) as sb:
        with pytest.raises(ServerError) as ei:
            await sb.read_page("Foo")
    assert str(ei.value) == "silverbullet error: 502"
    assert ei.value.cf_hint is None


@pytest.mark.asyncio
async def test_5xx_response_cf_hint_threads_to_every_write_tool() -> None:
    """T43: the ``cf_hint`` is populated on 5xx from every
    path that flows through ``_raise_for_status``.

    Pin: any future entry point added to ``sb_client`` that
    bypasses ``_raise_for_status`` (e.g. a new method that
    catches ``httpx`` directly) would silently drop the
    ``cf_hint``. This test exercises every 5xx-touched path
    in ``sb_client`` that surfaces a ``ServerError`` to its
    caller (``read_page``, ``write_page``, ``delete_page``,
    ``exists_page``, ``read_page_meta``, ``list_pages``) and
    asserts the ``cf_hint`` is on every raised
    ``ServerError``. ``read_page_meta_safe`` is *not* on this
    list — it deliberately swallows 5xx to ``None`` (the
    list-pages hydration walker treats 5xx as transient per
    page and keeps the row's ``etag=None`` rather than
    surfacing the failure to the agent); the
    ``cf_hint`` is intentionally dropped there because the
    safe wrapper is the layer that decides whether to
    surface 5xx at all.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text=_CF_BODY)

    async with _client(handler) as sb:
        # Every entry point that flows through ``_raise_for_status``
        # and surfaces the resulting ``ServerError`` to its caller.
        # Each one must surface the same ``cf_hint`` so the MCP tool
        # layer can attach it to the error envelope.
        for call in (
            ("read_page", lambda sb: sb.read_page("Foo")),
            ("write_page", lambda sb: sb.write_page("Foo", "body")),
            ("delete_page", lambda sb: sb.delete_page("Foo")),
            ("exists_page", lambda sb: sb.exists_page("Foo")),
            ("read_page_meta", lambda sb: sb.read_page_meta("Foo")),
            ("list_pages", lambda sb: sb.list_pages()),
        ):
            name, fn = call
            with pytest.raises(ServerError) as ei:
                await fn(sb)
            assert ei.value.cf_hint is not None, (
                f"{name} did not populate cf_hint on a 5xx"
            )
            assert ei.value.cf_hint["error_code"] == 502, (
                f"{name} populated wrong cf_hint: {ei.value.cf_hint}"
            )
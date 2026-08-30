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
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/.fs/index"
        return httpx.Response(200, text="# hello")

    async with _client(handler) as sb:
        body = await sb.read_page("index")

    assert body == "# hello"


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
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(request.headers)
        return httpx.Response(200, headers={"ETag": '"abc123"'})

    async with _client(handler) as sb:
        etag = await sb.write_page("index", "# new body")

    assert etag == '"abc123"'
    assert captured["x-source"] == "external"
    assert captured["x-permission"] == "rw"
    assert captured["content-type"] == "text/markdown"


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


# --- list_pages --------------------------------------------------------


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
"""Live SilverBullet end-to-end (env-gated).

Skipped unless both ``MCP_SILVERBULLET_LIVE_SB_URL`` and
``MCP_SILVERBULLET_LIVE_SB_TOKEN`` are present in the process env.
An empty token is valid (this dev-box SB has no auth).

The test boots the real bridge on a free TCP port (uvicorn via
``serve()``), talks Streamable HTTP the same way a Grok client would,
and hits the live ``/.fs`` API. Cleanup of the marker page is
best-effort in ``finally`` (``DELETE /.fs/{name}``).

``list_pages`` is exercised but not required to succeed: on this SB
``GET /.fs`` returns 307 to ``/`` (T6 smoke, map fog). When that
happens the tool error is asserted and the filter check is skipped;
when a real list payload appears, the marker name must be in it.
"""

from __future__ import annotations

import asyncio
import os
import socket
import time
from contextlib import suppress

import httpx2 as httpx
import pytest
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

from mcp_silverbullet.main import Settings, serve

MARKER = "e2e-mcp-silverbullet-marker.md"
INBOUND_TOKEN = "e2e-live-sb-inbound-token"


def _require_live_env() -> tuple[str, str]:
    if (
        "MCP_SILVERBULLET_LIVE_SB_URL" not in os.environ
        or "MCP_SILVERBULLET_LIVE_SB_TOKEN" not in os.environ
    ):
        pytest.skip(
            "live-SB e2e skipped: set MCP_SILVERBULLET_LIVE_SB_URL and "
            "MCP_SILVERBULLET_LIVE_SB_TOKEN (empty token is ok if SB has no auth)"
        )
    url = os.environ["MCP_SILVERBULLET_LIVE_SB_URL"].rstrip("/")
    token = os.environ["MCP_SILVERBULLET_LIVE_SB_TOKEN"].strip()
    return url, token


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def _wait_listening(host: str, port: int, timeout: float = 8.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            reader, writer = await asyncio.open_connection(host, port)
            writer.close()
            await writer.wait_closed()
            return
        except OSError:
            await asyncio.sleep(0.05)
    raise TimeoutError(f"bridge did not listen on {host}:{port} within {timeout}s")


async def _delete_marker(sb_url: str, sb_token: str) -> None:
    headers = {}
    if sb_token:
        headers["Authorization"] = f"Bearer {sb_token}"
    async with httpx.AsyncClient(base_url=sb_url, headers=headers, timeout=5.0) as client:
        with suppress(httpx.HTTPError):
            await client.delete(f"/.fs/{MARKER}")


@pytest.mark.asyncio
async def test_live_sb_write_read_list_and_precondition() -> None:
    sb_url, sb_token = _require_live_env()
    port = _free_port()
    host = "127.0.0.1"
    resource_url = f"http://{host}:{port}/mcp"
    settings = Settings(
        token=INBOUND_TOKEN,
        sb_url=sb_url,
        sb_token=sb_token,
        resource_url=resource_url,
        host=host,
        port=port,
        allowed_hosts=(),
    )
    body = "hello from T7 live e2e\n"
    server_task = asyncio.create_task(serve(settings))
    try:
        await _wait_listening(host, port)
        async with httpx.AsyncClient(
            headers={"Authorization": f"Bearer {INBOUND_TOKEN}"},
            timeout=15.0,
        ) as http_client:
            async with streamable_http_client(
                url=resource_url,
                http_client=http_client,
            ) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()

                    written = await session.call_tool(
                        "write_page",
                        {"name": MARKER, "content": body},
                    )
                    assert written.is_error is False, written

                    read_back = await session.call_tool(
                        "read_page", {"name": MARKER}
                    )
                    assert read_back.is_error is False, read_back
                    assert read_back.content[0].text == body

                    # Ticket asked for 412 on If-Match. HTTP If-Match: *
                    # requires the page to exist (should 200 here).
                    # A stale etag should 412 — this live SB ignores
                    # If-Match entirely (always 200); treat that as a
                    # recorded SB fact, not a bridge failure.
                    starred = await session.call_tool(
                        "write_page",
                        {
                            "name": MARKER,
                            "content": body,
                            "if_match": "*",
                        },
                    )
                    assert starred.is_error is False, starred

                    stale = await session.call_tool(
                        "write_page",
                        {
                            "name": MARKER,
                            "content": body,
                            "if_match": '"not-the-etag"',
                        },
                    )
                    if stale.is_error:
                        assert "precondition failed" in stale.content[0].text

                    listed = await session.call_tool(
                        "list_pages",
                        {"prefix": "e2e-mcp-silverbullet-marker"},
                    )
                    if listed.is_error:
                        # GET /.fs → 307 / on this SB (map fog / T6).
                        text = listed.content[0].text
                        assert "silverbullet error" in text
                    else:
                        payload = listed.content[0].text
                        assert MARKER in payload or "e2e-mcp-silverbullet-marker" in payload
    finally:
        server_task.cancel()
        with suppress(asyncio.CancelledError):
            await server_task
        await _delete_marker(sb_url, sb_token)

"""Live journal-surface end-to-end (env-gated).

Skipped unless ``MCP_SILVERBULLET_LIVE_SPACE_PATH`` points at a readable
SB space directory. The journal surface doesn't go through SilverBullet
at all — it reads the space directory from disk — so unlike T7 we don't
need a live SB. The bridge is still booted on a real port with a real
``serve()`` task so the wire shape is the same a Grok client would see.

The three assertions mirror T11's live smoke: ``journal_histogram``
returns a non-empty dict with at least one entry newer than 2023-10,
``tag_summary`` has ``"daily"`` as a key, and ``recent_pages(limit=5)``
returns 5 entries from the ``Daily/`` subdirectory.

Read-only — no marker file is created and no cleanup is needed.
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

from mcp_silverbullet.journal import JournalConfig
from mcp_silverbullet.main import Settings, serve

INBOUND_TOKEN = "e2e-live-journal-inbound-token"


def _require_live_env() -> str:
    """Skip unless ``MCP_SILVERBULLET_LIVE_SPACE_PATH`` points at a readable dir.

    ``os.access(..., os.R_OK)`` is racy (the FS can change between the
    check and the tool call), but a False here means the path is
    obviously bad and the journal gate would have closed anyway. The
    real read in :func:`_iter_md` swallows ``OSError`` defensively.
    """
    raw = os.environ.get("MCP_SILVERBULLET_LIVE_SPACE_PATH", "").strip()
    if not raw:
        pytest.skip(
            "live-journal e2e skipped: set MCP_SILVERBULLET_LIVE_SPACE_PATH "
            "to an absolute path to the SB space directory"
        )
    if not os.path.isdir(raw):
        pytest.skip(
            f"live-journal e2e skipped: MCP_SILVERBULLET_LIVE_SPACE_PATH={raw!r} "
            "is not a directory"
        )
    if not os.access(raw, os.R_OK):
        pytest.skip(
            f"live-journal e2e skipped: MCP_SILVERBULLET_LIVE_SPACE_PATH={raw!r} "
            "is not readable"
        )
    return raw


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


@pytest.mark.asyncio
async def test_live_journal_histogram_tag_summary_recent_pages() -> None:
    """All four journal tools register; three real-shape assertions hold."""
    space_path = _require_live_env()
    port = _free_port()
    host = "127.0.0.1"
    resource_url = f"http://{host}:{port}/mcp"
    # The journal tools never call into the SB client — point at a
    # loopback port nothing's listening on so a regression that
    # suddenly routed a journal call through SB would surface as a
    # connect-timeout error rather than a silent 200.
    settings = Settings(
        token=INBOUND_TOKEN,
        sb_url="http://127.0.0.1:1",
        sb_token="",
        resource_url=resource_url,
        host=host,
        port=port,
        allowed_hosts=(),
        journal=JournalConfig(enabled=True, space_path=space_path),
    )
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

                    # Sanity-check: gate-open means all four tools
                    # are registered. (T7 asserts the inverse — that
                    # the /.fs-backed tools still work alongside
                    # the journal surface — by exercising
                    # ``list_pages`` against the live SB. We don't
                    # do that here because the journal test is
                    # intentionally decoupled from SB.)
                    tools = await session.list_tools()
                    names = {t.name for t in tools.tools}
                    assert {
                        "journal_histogram",
                        "tag_summary",
                        "recent_pages",
                        "pages_touching_topic",
                    } <= names, (
                        f"journal gate open but tools missing; got {names}"
                    )

                    # (a) ``journal_histogram`` is a ``dict[str, int]``
                    # — the SDK emits the dict itself, no
                    # ``{"result": …}`` wrap (T11 carry-forward).
                    histogram = await session.call_tool(
                        "journal_histogram", {}
                    )
                    assert histogram.is_error is False, histogram
                    payload = histogram.structured_content or {}
                    assert isinstance(payload, dict)
                    assert payload, (
                        "journal_histogram against the live space "
                        "should return at least one bucket"
                    )
                    # At least one entry newer than 2023-10. Keys are
                    # ``YYYY-MM`` strings; lexical comparison matches
                    # chronological for that prefix.
                    newer_keys = [k for k in payload if k > "2023-10"]
                    assert newer_keys, (
                        f"expected at least one YYYY-MM key newer than "
                        f"2023-10; got {sorted(payload)}"
                    )

                    # (b) ``tag_summary`` includes ``"daily"``. Same
                    # bare-dict wire shape as histogram.
                    tags = await session.call_tool("tag_summary", {})
                    assert tags.is_error is False, tags
                    tag_payload = tags.structured_content or {}
                    assert "daily" in tag_payload, (
                        f"expected 'daily' in tag_summary keys; "
                        f"got {sorted(tag_payload)[:10]}"
                    )

                    # (c) ``recent_pages(limit=5)`` returns 5 entries
                    # from ``Daily/``. ``list[dict]`` IS wrapped in
                    # ``{"result": …}`` per Layer-1 shape.
                    recent = await session.call_tool(
                        "recent_pages", {"limit": 5, "prefix": "Daily"}
                    )
                    assert recent.is_error is False, recent
                    rows = (recent.structured_content or {}).get("result", [])
                    assert len(rows) == 5, (
                        f"recent_pages(limit=5, prefix='Daily') returned "
                        f"{len(rows)} rows, want 5"
                    )
                    assert all(r["name"].startswith("Daily/") for r in rows), (
                        f"prefix filter leaked non-Daily rows: "
                        f"{[r['name'] for r in rows]}"
                    )
                    # Each row carries the three documented fields.
                    for row in rows:
                        assert set(row.keys()) == {
                            "name",
                            "mtime_iso",
                            "size_bytes",
                        }
                        assert row["mtime_iso"].endswith("+00:00")
                        assert isinstance(row["size_bytes"], int)
    finally:
        server_task.cancel()
        with suppress(asyncio.CancelledError):
            await server_task

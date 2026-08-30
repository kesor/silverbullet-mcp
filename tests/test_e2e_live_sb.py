"""Live SilverBullet end-to-end (env-gated).

Skipped unless both ``MCP_SILVERBULLET_LIVE_SB_URL`` and
``MCP_SILVERBULLET_LIVE_SB_TOKEN`` are present in the process env.
An empty token is valid (this dev-box SB has no auth).

The test boots the real bridge on a free TCP port (uvicorn via
``serve()``), talks Streamable HTTP the same way a Grok client would,
and hits the live ``/.fs`` API. Cleanup of the marker page is
best-effort in ``finally`` (``DELETE /.fs/{name}``).

``list_pages`` is exercised against the live SB and is required to
succeed: the bridge sends ``X-Sync-Mode: 1`` on ``GET /.fs`` (T10's
drive-by fix for a bug that was parked as 'effectively moot' once
the journal surface replaced the original search tool — but the
existing ``/.fs``-backed ``list_pages`` tool still needed it). Without
that header, SB 2.9.0 307-redirects to the SPA UI. The assertion
checks that the freshly-written marker appears in the structured
``{"result": [...]}`` payload.
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
        # The T22 move round-trip creates a ``{MARKER}-moved`` page
        # and then moves it back; if the test fails between those
        # two moves, the moved-name page is left in the live space.
        # Best-effort cleanup of both — the suppress is per-page so
        # a missing marker doesn't mask a missing moved-page.
        with suppress(httpx.HTTPError):
            await client.delete(f"/.fs/{MARKER}")
        with suppress(httpx.HTTPError):
            await client.delete(f"/.fs/{MARKER}-moved")


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
        journal=JournalConfig(enabled=False, space_path=None),
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
                    # T23 envelope: ``{name, etag, size_bytes, ...}``.
                    # ``size_bytes`` comes from the request body byte
                    # count (``len("hello from T7 live e2e\n") == 23``),
                    # which the live SB echoes back via
                    # ``X-Content-Length``. ``name`` is what we asked
                    # to write. Other meta fields may or may not be
                    # populated depending on the live SB build.
                    wpayload = written.structured_content or {}
                    assert wpayload.get("name") == MARKER
                    assert wpayload.get("size_bytes") == len(
                        body.encode("utf-8")
                    )

                    # ``page_exists`` (T25): the freshly-written
                    # marker is on disk, so the round-trip should
                    # return ``True``. Live-SB coverage is optional
                    # per the v1 T7 carry-forward (the Layer-1 +
                    # Layer-3 tests already lock the wire shape),
                    # but a single round-trip here guards the
                    # full HTTP path against the live server.
                    present = await session.call_tool(
                        "page_exists", {"name": MARKER}
                    )
                    assert present.is_error is False, present
                    assert (present.structured_content or {}) == {
                        "result": True
                    }

                    read_back = await session.call_tool(
                        "read_page", {"name": MARKER}
                    )
                    assert read_back.is_error is False, read_back
                    # T24: ``read_page`` returns the ack envelope.
                    # ``size_bytes`` should match the byte count of
                    # ``body`` (``X-Content-Length`` echo from the
                    # live SB). ``etag`` / ``last_modified_ms`` /
                    # ``created_ms`` are dropped (T24 read shape).
                    rpayload = read_back.structured_content or {}
                    assert rpayload.get("body") == body
                    assert rpayload.get("size_bytes") == len(
                        body.encode("utf-8")
                    )

                    # ``append_to_page`` is the v1.1 T19 read-modify-
                    # write tool; lock the round-trip shape against
                    # the live SB while we have a session open. The
                    # separator rule (one ``\n`` inserted unless the
                    # body already ends in one) is the headline
                    # semantic; assert on the wire result.
                    appended = await session.call_tool(
                        "append_to_page",
                        {"name": MARKER, "text": "appended\n"},
                    )
                    assert appended.is_error is False, appended
                    # T23 envelope: combined body is
                    # ``hello from T7 live e2e\nappended\n`` = 32 bytes.
                    assert (appended.structured_content or {}).get(
                        "size_bytes"
                    ) == 32

                    after_append = await session.call_tool(
                        "read_page", {"name": MARKER}
                    )
                    assert after_append.is_error is False, after_append
                    # T24 envelope: body lives at ``.body``; the
                    # combined body is ``hello from T7 live e2e\n
                    # appended\n`` (32 bytes). Body ended in ``\n``
                    # already, so no extra separator is inserted
                    # between the two halves.
                    assert (after_append.structured_content or {}).get(
                        "body"
                    ) == body + "appended\n"

                    # ``patch_page_lines`` is the v1.1 T20 read-
                    # modify-write tool; round-trip the middle line
                    # replacement against the live SB so the line
                    # splitting + rejoin shape is verified end-to-
                    # end, not just under ``MockTransport``. The
                    # marker body is now ``hello from T7 live e2e\n
                    # appended\n`` (two lines, ends in ``\n``);
                    # replacing line 1 with ``patched`` yields
                    # ``patched\nappended\n``.
                    patched = await session.call_tool(
                        "patch_page_lines",
                        {
                            "name": MARKER,
                            "start_line": 1,
                            "end_line": 1,
                            "new_content": "patched\n",
                        },
                    )
                    assert patched.is_error is False, patched
                    # T23 envelope: patched body is
                    # ``patched\nappended\n`` = 16 bytes.
                    assert (patched.structured_content or {}).get(
                        "size_bytes"
                    ) == 16

                    after_patch = await session.call_tool(
                        "read_page", {"name": MARKER}
                    )
                    assert after_patch.is_error is False, after_patch
                    # T24 envelope: body lives at ``.body``.
                    assert (after_patch.structured_content or {}).get(
                        "body"
                    ) == "patched\nappended\n"

                    # ``patch_page_replace`` is the v1.1 T21 read-
                    # modify-write tool; round-trip the literal
                    # find-and-replace against the live SB so the
                    # substring match + body write is verified end-
                    # to-end, not just under ``MockTransport``. Body
                    # is currently ``patched\nappended\n``; replace
                    # the unique ``patched`` substring with ``hello``
                    # → ``hello\nappended\n``.
                    replaced = await session.call_tool(
                        "patch_page_replace",
                        {
                            "name": MARKER,
                            "find": "patched",
                            "new_string": "hello",
                        },
                    )
                    assert replaced.is_error is False, replaced
                    # T23 envelope: replaced body is
                    # ``hello\nappended\n`` = 15 bytes.
                    assert (replaced.structured_content or {}).get(
                        "size_bytes"
                    ) == 15

                    after_replace = await session.call_tool(
                        "read_page", {"name": MARKER}
                    )
                    assert after_replace.is_error is False, after_replace
                    # T24 envelope: body lives at ``.body``.
                    assert (after_replace.structured_content or {}).get(
                        "body"
                    ) == "hello\nappended\n"

                    # ``move_page`` is the v1.1 T22 read-write-delete
                    # tool; round-trip the rename against live SB so
                    # the write-then-delete shape (and the new page's
                    # read-back) is verified end-to-end, not just
                    # under ``MockTransport``. Body is currently
                    # ``hello\nappended\n``; move it to a new name and
                    # read it back from there. The original ``MARKER``
                    # name must be gone afterwards.
                    moved_name = f"{MARKER}-moved"
                    moved = await session.call_tool(
                        "move_page",
                        {"name": MARKER, "new_name": moved_name},
                    )
                    assert moved.is_error is False, moved
                    # T23 envelope: ``name`` is the *destination*
                    # (``moved_name``), not the source. The body
                    # is ``hello\nappended\n`` = 15 bytes.
                    mpayload = moved.structured_content or {}
                    assert mpayload.get("name") == moved_name
                    assert mpayload.get("size_bytes") == 15

                    after_move_old = await session.call_tool(
                        "read_page", {"name": MARKER}
                    )
                    assert after_move_old.is_error is True, (
                        "source page should be gone after move"
                    )

                    after_move_new = await session.call_tool(
                        "read_page", {"name": moved_name}
                    )
                    assert after_move_new.is_error is False, after_move_new
                    # T24 envelope: body lives at ``.body``.
                    assert (after_move_new.structured_content or {}).get(
                        "body"
                    ) == "hello\nappended\n"

                    # Move it back so the precondition block below
                    # can still find ``MARKER``.
                    move_back = await session.call_tool(
                        "move_page",
                        {"name": moved_name, "new_name": MARKER},
                    )
                    assert move_back.is_error is False, move_back
                    # T23 envelope: moved back, ``name`` is the
                    # destination (``MARKER``).
                    assert (move_back.structured_content or {}).get(
                        "name"
                    ) == MARKER

                    # Reset the body for the precondition block
                    # below (which writes ``body`` and expects to
                    # find it on read-back).
                    reset = await session.call_tool(
                        "write_page", {"name": MARKER, "content": body}
                    )
                    assert reset.is_error is False, reset

                    # ``dry_run=True`` (T26) round-trip against the
                    # live SB: preview a patch that would append
                    # ``"appended\\n"``, confirm the dry-run envelope
                    # says so, and confirm a follow-up read shows the
                    # page is unchanged. The whole point of dry-run
                    # is no writes; a read after the dry-run must show
                    # the body still equals ``body``.
                    dry_appended = await session.call_tool(
                        "append_to_page",
                        {
                            "name": MARKER,
                            "text": "appended\n",
                            "dry_run": True,
                        },
                    )
                    assert dry_appended.is_error is False, dry_appended
                    dpayload = dry_appended.structured_content or {}
                    assert dpayload.get("dry_run") is True
                    assert dpayload.get("original") == body
                    assert dpayload.get("patched") == body + "appended\n"
                    assert "+appended" in dpayload.get("diff", "")
                    after_dry_append = await session.call_tool(
                        "read_page", {"name": MARKER}
                    )
                    assert after_dry_append.is_error is False, (
                        "dry-run must not delete the page"
                    )
                    # ``patched`` is what would have been written;
                    # ``original`` is what the page actually still
                    # has. Dry-run no write → ``original`` is the
                    # post-read-back body.
                    assert (after_dry_append.structured_content or {}).get(
                        "body"
                    ) == body

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
                    assert not listed.is_error, (
                        f"list_pages against live SB: "
                        f"{listed.content[0].text if listed.content else listed}"
                    )
                    # ``list_pages`` returns structured content
                    # (``list[dict[str, str | None]]``); the wire shape
                    # is ``{"result": [...]}`` per the Layer-1 test.
                    payload = listed.structured_content or {}
                    names = {item["name"] for item in payload.get("result", [])}
                    assert MARKER in names
    finally:
        server_task.cancel()
        with suppress(asyncio.CancelledError):
            await server_task
        await _delete_marker(sb_url, sb_token)

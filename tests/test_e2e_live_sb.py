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
        with suppress(httpx.HTTPError):
            # T27's diff_pages round-trip may leave a copy page
            # behind if the test crashes between the diff and its
            # cleanup. Best-effort delete so the live space stays
            # clean.
            await client.delete(f"/.fs/{MARKER}-diff")


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
        list_pages_hydrate_etags=False,
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
                    # ``patched\nappended\n`` = 17 bytes (UTF-8).
                    assert (patched.structured_content or {}).get(
                        "size_bytes"
                    ) == 17

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
                    # (T28: ``list[{name, etag, size_bytes,
                    # last_modified_ms, created_ms}]`` — the same
                    # envelope family the read/write tools use);
                    # the wire shape is ``{"result": [...]}`` per
                    # the Layer-1 test. The assertion only reads
                    # each row's ``name`` so the shape widening
                    # is transparent to this test (the
                    # ``name`` field position is unchanged).
                    payload = listed.structured_content or {}
                    names = {item["name"] for item in payload.get("result", [])}
                    assert MARKER in names

                    # ``diff_pages`` (T27) round-trip against live SB:
                    # diff ``MARKER`` against a literal string
                    # (``other_body``) so the wire shape with
                    # ``other=None`` is exercised; the literal string
                    # has to differ from the page body so the diff is
                    # non-empty. We don't assert on the exact diff
                    # text — ``difflib.unified_diff``'s header format
                    # is the caller's responsibility, and the Layer-1
                    # tests already lock the structural pieces. We do
                    # assert the envelope carries the first-page
                    # ``name`` envelope and ``other=None`` for the
                    # literal-string case.
                    diffed = await session.call_tool(
                        "diff_pages",
                        {
                            "name": MARKER,
                            "other_body": "hello from T7 live e2e\nNOT-APPENDED\n",
                        },
                    )
                    assert diffed.is_error is False, diffed
                    dpayload = diffed.structured_content or {}
                    assert dpayload.get("other") is None
                    assert dpayload.get("name", {}).get("body") == body
                    assert "-appended\n" in dpayload.get("diff", "")
                    assert "+NOT-APPENDED\n" in dpayload.get("diff", "")

                    # ``diff_pages`` against a second page
                    # (``other_name``) — write a copy of ``MARKER``
                    # to a fresh name, diff them, then clean up.
                    # Same-name diffs surface as ``diff=""`` per the
                    # Layer-1 contract; the Layer-3 path we want to
                    # verify here is the round-trip envelope shape
                    # (``name`` and ``other`` both populated) plus
                    # the prefix-filter-irrelevant two-read shape
                    # against live SB.
                    diff_target = f"{MARKER}-diff"
                    await session.call_tool(
                        "write_page",
                        {"name": diff_target, "content": body + "appended\n"},
                    )
                    diffed_pages = await session.call_tool(
                        "diff_pages",
                        {"name": MARKER, "other_name": diff_target},
                    )
                    assert diffed_pages.is_error is False, diffed_pages
                    dp2 = diffed_pages.structured_content or {}
                    assert dp2.get("name", {}).get("body") == body
                    assert dp2.get("other", {}).get("body") == body + "appended\n"
                    # No-op diff is the case the agent will hit when
                    # ``MARKER`` and ``diff_target`` end up identical
                    # (e.g. after the dry-run block above didn't
                    # actually write); the contract is ``diff=""``.
                    # Both envelopes still surface so the caller has
                    # the etag from either side for an ``if_match``
                    # round-trip.
                    same_diff = await session.call_tool(
                        "diff_pages",
                        {"name": MARKER, "other_name": MARKER},
                    )
                    assert same_diff.is_error is False, same_diff
                    assert (same_diff.structured_content or {}).get("diff") == ""
                    # Cleanup the diff target so the precondition
                    # block above isn't affected.
                    await session.call_tool(
                        "delete_page", {"name": diff_target}
                    )

                    # ``list_tasks`` (T29) round-trip against live SB:
                    # overwrite the marker with a body that has
                    # checkbox bullets (one addressable with a
                    # wikilink ref, one non-addressable, one in the
                    # ``[X]`` cancelled state), then list tasks
                    # and assert the wire shape matches the
                    # Layer-1 contract — one entry per bullet,
                    # ``name`` echoes the page name, ``state`` is
                    # the literal checkbox character, ``ref`` is the
                    # wikilink target (or ``None`` for the non-
                    # addressable bullet), and ``line`` is the
                    # 1-indexed editor line. The ``list_tasks`` live
                    # round-trip is structurally identical to the
                    # ``read_page`` round-trip (one GET per page)
                    # plus the parser — so a green ``read_page``
                    # round-trip plus a green Layer-1
                    # ``list_tasks`` test is a strong live-SB
                    # signal; this block confirms the parser
                    # handles the real SB's file encoding and
                    # bullet rendering in the wild.
                    bullet_body = (
                        "hello from T7 live e2e\n"
                        "# Tasks\n"
                        "- [ ] first task [[FirstTask]]\n"
                        "- [x] done task\n"
                        "- [X] cancelled task\n"
                        "- [ ] non-addressable\n"
                    )
                    await session.call_tool(
                        "write_page",
                        {"name": MARKER, "content": bullet_body},
                    )
                    listed_tasks = await session.call_tool(
                        "list_tasks", {"page": MARKER}
                    )
                    assert listed_tasks.is_error is False, listed_tasks
                    tasks_payload = (
                        listed_tasks.structured_content or {}
                    ).get("result", [])
                    # Three of the four bullets have addressable
                    # states: todo / done / cancelled. The fourth
                    # (``non-addressable``) is also todo, just
                    # without a wikilink ref.
                    assert [t["state"] for t in tasks_payload] == [
                        " ", "x", "X", " "
                    ]
                    assert [t["ref"] for t in tasks_payload] == [
                        "FirstTask", None, None, None
                    ]
                    # ``name`` echoes the page name (parallel
                    # with the space-walk form's per-row ``name``)
                    # and ``line`` is 1-indexed against the full
                    # body. The first task is on editor line 2
                    # (line 1 is the prose header). We don't
                    # pin the rest of the line numbers — a future
                    # test that adds a body line above the
                    # bullets will shift them.
                    assert all(t["name"] == MARKER for t in tasks_payload)
                    assert tasks_payload[0]["line"] == 2

                    # ``check_task`` (T30) round-trip against live
                    # SB: flip ``FirstTask`` from ``[ ]`` (todo) to
                    # ``[x]`` (done), confirm the page body reflects
                    # the flip via a follow-up ``list_tasks`` /
                    # ``read_page``, then flip it back to ``[ ]``
                    # (``state=\"todo\"``). The T30 contract is:
                    # the read-modify-write round trip lands with
                    # the new marker, the etag chain keeps the
                    # write idempotent against a single concurrent
                    # editor save (we don't exercise that here —
                    # that's a fault-injection test the upstream
                    # ``append_to_page`` / ``patch_page_*`` live
                    # blocks already cover; ``check_task`` shares
                    # the same ``If-Match`` plumbing). We also
                    # exercise the ``dry_run=True`` path: a flip
                    # preview that leaves the page body unchanged
                    # so the live-SB signal for the no-PUT
                    # contract is structural rather than just
                    # load-bearing on the Layer-1 tests.
                    flipped = await session.call_tool(
                        "check_task",
                        {"page": MARKER, "ref": "FirstTask", "state": "done"},
                    )
                    assert flipped.is_error is False, flipped
                    ack = flipped.structured_content or {}
                    # T23 ack envelope on the write — etag,
                    # size_bytes, last_modified_ms all populated
                    # against real SB.
                    assert ack.get("name") == MARKER
                    assert ack.get("etag") is not None
                    assert ack.get("size_bytes") is not None
                    assert ack.get("last_modified_ms") is not None
                    # Confirm the page body now shows ``[x]`` for
                    # ``FirstTask`` via ``list_tasks`` — the
                    # bullet's ``state`` should have flipped.
                    listed_after = await session.call_tool(
                        "list_tasks", {"page": MARKER}
                    )
                    assert listed_after.is_error is False
                    after_tasks = (
                        listed_after.structured_content or {}
                    ).get("result", [])
                    first = next(
                        t for t in after_tasks if t["ref"] == "FirstTask"
                    )
                    assert first["state"] == "x"
                    # dry-run preview: a fully-built dry-run
                    # envelope, page body unchanged. We flip the
                    # same task back to ``todo`` via dry-run so
                    # the live-SB signal for the no-PUT contract
                    # is that the body's first task is still
                    # ``[x]`` after the dry-run call.
                    preview = await session.call_tool(
                        "check_task",
                        {
                            "page": MARKER,
                            "ref": "FirstTask",
                            "state": "todo",
                            "dry_run": True,
                        },
                    )
                    assert preview.is_error is False, preview
                    pc = preview.structured_content or {}
                    assert pc.get("dry_run") is True
                    assert "FirstTask" in (pc.get("patched") or "")
                    # Body unchanged: first task still ``[x]``.
                    listed_dry = await session.call_tool(
                        "list_tasks", {"page": MARKER}
                    )
                    assert listed_dry.is_error is False
                    dry_tasks = (
                        listed_dry.structured_content or {}
                    ).get("result", [])
                    first_dry = next(
                        t for t in dry_tasks if t["ref"] == "FirstTask"
                    )
                    assert first_dry["state"] == "x"
                    # Roll the real flip back to ``[ ]`` so the
                    # test leaves the live space in a known
                    # state (the rest of the test suite, future
                    # runs, and the operator's live state all
                    # benefit from a clean marker page).
                    rolled_back = await session.call_tool(
                        "check_task",
                        {"page": MARKER, "ref": "FirstTask", "state": "todo"},
                    )
                    assert rolled_back.is_error is False, rolled_back
                    listed_final = await session.call_tool(
                        "list_tasks", {"page": MARKER}
                    )
                    assert listed_final.is_error is False
                    final_tasks = (
                        listed_final.structured_content or {}
                    ).get("result", [])
                    first_final = next(
                        t for t in final_tasks if t["ref"] == "FirstTask"
                    )
                    assert first_final["state"] == " "
    finally:
        server_task.cancel()
        with suppress(asyncio.CancelledError):
            await server_task
        await _delete_marker(sb_url, sb_token)


# ---------------------------------------------------------------------------
# T31 verification: does SB actually enforce ``If-Match`` on
# ``PUT /.fs/{name}``? The v1.2 design has assumed this since T23; the
# map's T31 ticket resolves the assumption by writing a test that fails
# loudly if it doesn't hold. The shape follows the ticket verbatim:
# create a marker, read it twice (the ticket's "read twice" framing is
# kept even though most SBs return the same etag both times — the
# double-read is what the map said to do), write with the first etag
# (must 200, no precondition expected), then mutate the page
# out-of-band so the first etag is genuinely stale, then write again
# with that same first etag and assert 412-equivalent ``ToolError``.
#
# The out-of-band mutation step is load-bearing: the only way to
# guarantee ``etag_a`` is stale is to write the page through a path
# that doesn't carry ``If-Match: etag_a``. The bridge's own
# ``write_page(..., if_match=None)`` works for that (the
# ``If-Match: <read_etag>`` threading only kicks in when the caller
# doesn't pass one — see the v1.2 ``check_task`` / ``append_to_page``
# carry-forwards); we use it here so the test exercises the bridge
# end-to-end.
#
# This test is separate from the soft-recording precondition block in
# ``test_live_sb_write_read_list_and_precondition``: that block was
# correct *at T7* (we didn't know yet whether SB honors ``If-Match``,
# so it recorded the SB fact rather than asserting). T31 is the
# verification step that promotes the assumption to a checked
# invariant; the soft block stays as-is so the git history captures
# the v1.2 → v1.3 evolution cleanly.
# ---------------------------------------------------------------------------

T31_MARKER = "e2e-mcp-silverbullet-t31-if-match.md"


async def _delete_t31_marker(sb_url: str, sb_token: str) -> None:
    headers: dict[str, str] = {}
    if sb_token:
        headers["Authorization"] = f"Bearer {sb_token}"
    async with httpx.AsyncClient(base_url=sb_url, headers=headers, timeout=5.0) as client:
        with suppress(httpx.HTTPError):
            await client.delete(f"/.fs/{T31_MARKER}")


@pytest.mark.asyncio
async def test_if_match_stale_etag_returns_412() -> None:
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
        list_pages_hydrate_etags=False,
    )
    body_a = "T31 first write body\n"
    body_b = "T31 second write body (no If-Match; mutates out-of-band)\n"
    body_c = "T31 third write body with stale If-Match; should 412\n"
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

                    # Step 1: create the marker.
                    created = await session.call_tool(
                        "write_page",
                        {"name": T31_MARKER, "content": body_a},
                    )
                    assert created.is_error is False, created
                    created_payload = created.structured_content or {}
                    etag_a = created_payload.get("etag")
                    last_modified_a = created_payload.get("last_modified_ms")
                    size_a = created_payload.get("size_bytes")
                    if etag_a is None:
                        # The dev-box SB returns no ``ETag`` response
                        # header on PUT — a pre-existing SB fact the
                        # v1.1 T22 / v1.2 T7 resolutions recorded.
                        # T31 cannot thread a real etag without one,
                        # so we fall back to the next-best synthetic:
                        # ``"<last_modified_ms>-<size_bytes>"``. The
                        # bridge forwards whatever the caller passes
                        # in ``if_match`` verbatim, so a synthetic
                        # value is structurally equivalent to a real
                        # etag for the purposes of this test (the
                        # question is whether SB honors the
                        # ``If-Match`` header *at all*, not whether
                        # the value looks like a real etag). The
                        # synthetic etag is *guaranteed* to drift on
                        # the mutating write below (since
                        # ``size_bytes`` changes), so the stale-write
                        # step is a real concurrency test either way.
                        if last_modified_a is None or size_a is None:
                            raise AssertionError(
                                "T31 cannot construct a fallback etag: "
                                "live SB stripped both ETag and "
                                "X-Last-Modified on the PUT response. "
                                "T31 is unrunnable against this SB "
                                "build; the v1.2 / v1.3 concurrency "
                                "story has no header to thread; T31 "
                                "closes negatively on the missing "
                                "ETag finding alone."
                            )
                        # T44: the synthesized form is now
                        # ``"{size_bytes}"`` alone — the
                        # ``X-Last-Modified`` component was dropped
                        # because the bridge stamps it with ``now_ms``
                        # on every PUT, which made the dashed form
                        # drift on every write and caused T31b's
                        # post-write verification to raise a
                        # false-positive "concurrent edit detected"
                        # on every successful write.
                        etag_a = f'"{size_a}"'

                    # Step 2: read twice (the ticket's "read twice"
                    # framing — most SBs return the same etag both
                    # times; the second read is kept so the test
                    # matches the spec exactly).
                    read_1 = await session.call_tool(
                        "read_page", {"name": T31_MARKER}
                    )
                    assert read_1.is_error is False, read_1
                    read_2 = await session.call_tool(
                        "read_page", {"name": T31_MARKER}
                    )
                    assert read_2.is_error is False, read_2

                    # Step 3: write with ``if_match=etag_a`` (still
                    # current, must succeed).
                    first_write = await session.call_tool(
                        "write_page",
                        {
                            "name": T31_MARKER,
                            "content": body_a,
                            "if_match": etag_a,
                        },
                    )
                    assert first_write.is_error is False, (
                        f"If-Match with a current etag should 200; "
                        f"got is_error=True: {first_write}"
                    )

                    # Step 4: mutate the page out-of-band so
                    # ``etag_a`` is *guaranteed* stale. We do this
                    # through the bridge with ``if_match=None`` (no
                    # precondition) so the bridge layer doesn't see
                    # the second write as a no-op retry. After the
                    # mutation, ``etag_a`` is stale whether SB
                    # returned a real etag (which would have drifted)
                    # or we synthesized one from
                    # ``last_modified_ms + size_bytes`` (which
                    # *guarantees* drift on a different body length).
                    mutating_write = await session.call_tool(
                        "write_page",
                        {"name": T31_MARKER, "content": body_b},
                    )
                    assert mutating_write.is_error is False, mutating_write

                    # Step 5: write again with the *stale* etag.
                    # The bridge threads ``If-Match: etag_a`` to
                    # SB; an SB that honors preconditions returns
                    # 412, which :func:`_translate_sb_errors`
                    # surfaces as ``ToolError("precondition
                    # failed; check if_match/if_none_match")``. An
                    # SB that ignores ``If-Match`` returns 200 and
                    # silently overwrites — the failure mode the
                    # v1.2 / v1.3 concurrency story relies on
                    # *not* happening. The hard assert below is
                    # the verification: pass = T31 positive,
                    # fail = T31 negative (which spawns T31a /
                    # T31b per the map).
                    stale_write = await session.call_tool(
                        "write_page",
                        {
                            "name": T31_MARKER,
                            "content": body_c,
                            "if_match": etag_a,
                        },
                    )
                    assert stale_write.is_error is True, (
                        f"stale If-Match etag should 412 on "
                        f"PUT /.fs/{T31_MARKER}; bridge returned "
                        f"is_error=False (live SB silently "
                        f"overwrote the page on a stale etag — "
                        f"the v1.2 / v1.3 concurrency story "
                        f"does not hold on this SB build; T31 "
                        f"closes negatively; T31a / T31b spawn "
                        f"to switch to the body-field "
                        f"expected_last_modified convention "
                        f"from xmatthewx/silverbullet-mcp-server)"
                    )
                    # The wording is the unified 412 translation
                    # in :func:`_translate_sb_errors`. We assert
                    # on the substring so future wording tweaks
                    # don't break the test; the full message is
                    # logged above on failure.
                    assert stale_write.content, (
                        "is_error=True but content is empty — "
                        "no message to check the 412 wording against"
                    )
                    assert "precondition failed" in (
                        stale_write.content[0].text
                    ), (
                        f"stale If-Match raised a non-412 error: "
                        f"{stale_write.content[0].text!r}"
                    )
    finally:
        server_task.cancel()
        with suppress(asyncio.CancelledError):
            await server_task
        await _delete_t31_marker(sb_url, sb_token)


# ---------------------------------------------------------------------------
# T31a: synthetic-etag fallback end-to-end against live SB.
#
# The T31 verification closed negatively on this dev box: SB strips
# the ``ETag`` response header on PUT, so a real-etag round trip is
# impossible. T31a's ``synthesize_etag`` fallback derives a stable
# value from ``X-Last-Modified`` + ``X-Content-Length``. This live
# test exercises the fallback end-to-end: read a page (synthesized
# etag appears on the envelope even though SB stripped ``ETag``),
# then write with that synthesized value, then read again with a
# different body length and assert the synthesized etag drifted.
# This is the operational canary that the v1.3 fallback is wired
# correctly: if the synthesized value doesn't drift on a real body
# change, T31b's post-write verification has nothing to compare.
# ---------------------------------------------------------------------------

T31A_MARKER = "e2e-mcp-silverbullet-t31a-synth-etag.md"


async def _delete_t31a_marker(sb_url: str, sb_token: str) -> None:
    headers: dict[str, str] = {}
    if sb_token:
        headers["Authorization"] = f"Bearer {sb_token}"
    async with httpx.AsyncClient(base_url=sb_url, headers=headers, timeout=5.0) as client:
        with suppress(httpx.HTTPError):
            await client.delete(f"/.fs/{T31A_MARKER}")


@pytest.mark.asyncio
async def test_if_match_synthetic_etag_drifts_on_body_change() -> None:
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
        list_pages_hydrate_etags=False,
    )
    body_a = "T31a first write body\n"
    body_b = "T31a second write body, longer\n"
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

                    # Step 1: write body_a. The envelope's ``etag``
                    # is synthesized from ``X-Last-Modified`` +
                    # ``X-Content-Length`` because SB strips
                    # ``ETag`` on this build (the T31 fact). The
                    # synthesized value is whatever
                    # :func:`synthesize_etag` returns for the
                    # response headers — what matters for this test
                    # is that the envelope carries *some* etag so
                    # the agent has a value to thread into a
                    # follow-up write.
                    created = await session.call_tool(
                        "write_page",
                        {"name": T31A_MARKER, "content": body_a},
                    )
                    assert created.is_error is False, created
                    created_payload = created.structured_content or {}
                    etag_a = created_payload.get("etag")
                    size_a = created_payload.get("size_bytes")
                    assert etag_a is not None, (
                        "T31a: bridge envelope on a fresh write "
                        "should carry *some* etag (real or "
                        "synthesized); got None on this SB build. "
                        "synthesize_etag fallback is not wired "
                        "correctly."
                    )
                    assert size_a is not None
                    # The synthesized form on this SB build is
                    # ``"<bytes>"`` (T44 — pre-T44 the form was
                    # ``"<ms>-<bytes>"``; the mtime component was
                    # dropped because the bridge stamps
                    # ``X-Last-Modified`` with ``now_ms`` on every
                    # PUT, which made the dashed form drift on
                    # every write and caused T31b's
                    # post-write verification to raise a
                    # false-positive "concurrent edit detected"
                    # on every successful write). Lock the exact
                    # shape so a future regression that changes
                    # the synthetic form (e.g. re-adding the
                    # mtime component) fails loudly.
                    assert etag_a.startswith('"') and etag_a.endswith('"')
                    # Strip quotes and assert the body is a
                    # numeric byte count — the only thing the
                    # synthesized form tracks post-T44.
                    assert etag_a.strip('"').isdigit(), (
                        f"T31a / T44: synthesized etag should be "
                        f"a quoted numeric byte count; got "
                        f"{etag_a!r}"
                    )

                    # Step 2: re-read the same body. The
                    # synthesized etag should match the post-write
                    # etag (stability across reads of unchanged
                    # content — the precondition an agent needs
                    # to detect *no* edit between read and write).
                    read_again = await session.call_tool(
                        "read_page", {"name": T31A_MARKER}
                    )
                    assert read_again.is_error is False, read_again
                    etag_a_read = (
                        read_again.structured_content or {}
                    ).get("etag")
                    assert etag_a_read == etag_a, (
                        f"T31a: synthetic etag should be stable "
                        f"across re-reads of the same body; got "
                        f"{etag_a_read!r} after first read, "
                        f"{etag_a!r} after write"
                    )

                    # Step 3: write body_b (different length). The
                    # synthesized etag *must* drift — it's the
                    # only signal T31b's post-write verification
                    # has to compare against. Without drift here,
                    # T31b's helper can never detect a concurrent
                    # edit on this SB build.
                    second_write = await session.call_tool(
                        "write_page",
                        {"name": T31A_MARKER, "content": body_b},
                    )
                    assert second_write.is_error is False, second_write
                    etag_b = (
                        second_write.structured_content or {}
                    ).get("etag")
                    assert etag_b is not None
                    assert etag_b != etag_a, (
                        f"T31a: synthetic etag should drift on a "
                        f"body change; got the same value "
                        f"{etag_b!r} for both writes. The "
                        f"synthesize_etag fallback is "
                        f"non-functional on this SB build."
                    )
    finally:
        server_task.cancel()
        with suppress(asyncio.CancelledError):
            await server_task
        await _delete_t31a_marker(sb_url, sb_token)


# ---------------------------------------------------------------------------
# T31b: post-write concurrency-token verification end-to-end against live SB.
#
# T31 closed negatively on this SB build: PUT returns 200 on
# stale ``If-Match`` (the v1.3 SB-blocking fact). Without T31b
# the v1.2 / v1.3 concurrency story was unsupported here. T31b's
# post-write verification helper re-reads after a 200 write and
# compares the post-write etag against the one the caller passed
# in ``if_match``; a mismatch raises
# ``ToolError("concurrent edit detected: …")``.
#
# This live test exercises the same race as T31 but with the
# helper in place: a write-then-mutate-out-of-band-then-write
# sequence against the live SB, where the second write's
# ``if_match`` is now stale. The T31 negative verification expected
# ``is_error=True`` on the second write's *412 path* (which never
# fires here); T31b's verification expects ``is_error=True`` on
# the helper's post-write re-read path instead. End state is the
# same — the agent sees ``concurrent edit detected`` — but the
# mechanism differs (post-write re-read, not pre-write 412).
# ---------------------------------------------------------------------------

T31B_MARKER = "e2e-mcp-silverbullet-t31b-verify.md"


async def _delete_t31b_marker(sb_url: str, sb_token: str) -> None:
    headers: dict[str, str] = {}
    if sb_token:
        headers["Authorization"] = f"Bearer {sb_token}"
    async with httpx.AsyncClient(base_url=sb_url, headers=headers, timeout=5.0) as client:
        with suppress(httpx.HTTPError):
            await client.delete(f"/.fs/{T31B_MARKER}")


@pytest.mark.asyncio
async def test_concurrent_edit_detected_via_post_write_verification() -> None:
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
        list_pages_hydrate_etags=False,
    )
    body_a = "T31b first write body\n"
    body_b = "T31b second write body, longer\n"
    body_c = "T31b third write body with stale If-Match\n"
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

                    # Step 1: write body_a. The envelope's
                    # ``etag`` is synthesized via T31a (SB
                    # strips ``ETag`` on this build). Capture
                    # the etag and the size for the synthetic
                    # fallback path; T31a already verified the
                    # synthesized value is stable across reads
                    # of unchanged content.
                    created = await session.call_tool(
                        "write_page",
                        {"name": T31B_MARKER, "content": body_a},
                    )
                    assert created.is_error is False, created
                    created_payload = created.structured_content or {}
                    etag_a = created_payload.get("etag")
                    last_modified_a = created_payload.get("last_modified_ms")
                    size_a = created_payload.get("size_bytes")
                    if etag_a is None:
                        # Same fallback as T31: synthesize a
                        # ``"{bytes}"`` value from ``size_bytes``
                        # the envelope *did* surface. T31a / T44
                        # now does this automatically on the
                        # bridge side, so this branch is
                        # unreachable in normal operation — kept
                        # as a defensive guard so a future
                        # regression that strips T31a's fallback
                        # doesn't crash this test. (Pre-T44 the
                        # synthesized form was
                        # ``"<ms>-<bytes>"``; T44 dropped the
                        # mtime component because the bridge
                        # stamps ``X-Last-Modified`` with
                        # ``now_ms`` on every PUT, which made
                        # the dashed form drift on every write
                        # and caused T31b's verification to
                        # raise a false-positive "concurrent
                        # edit detected" on every successful
                        # write.)
                        if last_modified_a is None or size_a is None:
                            raise AssertionError(
                                "T31b: cannot construct a synthetic "
                                "fallback etag; envelope missing both "
                                "etag and X-Last-Modified/size_bytes"
                            )
                        etag_a = f'"{size_a}"'

                    # Step 2: write body_b (mutate out-of-band
                    # so ``etag_a`` is stale). No ``if_match``
                    # here — this is the concurrent edit we're
                    # trying to detect.
                    mutating_write = await session.call_tool(
                        "write_page",
                        {"name": T31B_MARKER, "content": body_b},
                    )
                    assert mutating_write.is_error is False, (
                        "T31b: out-of-band mutating write failed; "
                        "test setup broken"
                    )

                    # Step 3: write body_c with the now-stale
                    # ``if_match=etag_a``. On this SB build the
                    # PUT returns 200 (T31's negative finding);
                    # T31b's post-write verification helper
                    # re-reads and detects the drift. The agent
                    # sees the unified
                    # ``"concurrent edit detected: …"``
                    # ``ToolError`` rather than a silent
                    # overwrite.
                    stale_write = await session.call_tool(
                        "write_page",
                        {
                            "name": T31B_MARKER,
                            "content": body_c,
                            "if_match": etag_a,
                        },
                    )
                    assert stale_write.is_error is True, (
                        f"T31b: stale If-Match should surface "
                        f"as 'concurrent edit detected' on this "
                        f"SB build (T31 negative; T31b fallback "
                        f"verification); bridge returned "
                        f"is_error=False. The v1.3 concurrency "
                        f"story is broken end-to-end."
                    )
                    assert stale_write.content, (
                        "is_error=True but content is empty — "
                        "no message to check the wording against"
                    )
                    assert "concurrent edit detected" in (
                        stale_write.content[0].text
                    ), (
                        f"T31b: stale write raised a "
                        f"non-concurrency error: "
                        f"{stale_write.content[0].text!r}"
                    )
                    assert etag_a in stale_write.content[0].text, (
                        f"T31b: error message should name the "
                        f"expected etag; got "
                        f"{stale_write.content[0].text!r}"
                    )
    finally:
        server_task.cancel()
        with suppress(asyncio.CancelledError):
            await server_task
        await _delete_t31b_marker(sb_url, sb_token)


# ---------------------------------------------------------------------------
# T32: create_page end-to-end against live SB.
#
# The T32 ticket's "Done when" calls for a T7-style live
# create-on-empty round trip. This test exercises the happy
# path end-to-end: create a fresh page, verify the returned
# envelope matches the T23 shape (``name`` / ``etag`` /
# ``size_bytes`` / ``last_modified_ms`` / ``created_ms``),
# then read it back to confirm the body landed. The
# page-already-exists case is exercised at Layer 1
# (``tests/test_tools_in_memory.py``); the live test pins
# the happy-path wire shape and the create-then-read
# round trip on a real SB.
# ---------------------------------------------------------------------------

T32_MARKER = "e2e-mcp-silverbullet-t32-create.md"


async def _delete_t32_marker(sb_url: str, sb_token: str) -> None:
    headers: dict[str, str] = {}
    if sb_token:
        headers["Authorization"] = f"Bearer {sb_token}"
    async with httpx.AsyncClient(base_url=sb_url, headers=headers, timeout=5.0) as client:
        with suppress(httpx.HTTPError):
            await client.delete(f"/.fs/{T32_MARKER}")


@pytest.mark.asyncio
async def test_create_page_round_trip_on_empty_space() -> None:
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
        list_pages_hydrate_etags=False,
    )
    body = "T32 create page body\n"
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

                    # Step 1: create the page. The envelope
                    # carries the T23 wire shape (``name`` /
                    # ``etag`` / ``size_bytes`` /
                    # ``last_modified_ms`` / ``created_ms``);
                    # ``is_error`` is False on the happy path.
                    created = await session.call_tool(
                        "create_page",
                        {"name": T32_MARKER, "content": body},
                    )
                    assert created.is_error is False, created
                    created_payload = created.structured_content or {}
                    assert created_payload.get("name") == T32_MARKER
                    # ``size_bytes`` is the UTF-8 byte count
                    # of the body we just wrote (matches the
                    # T23 contract; independent of SB's
                    # response shape on this build).
                    assert created_payload.get("size_bytes") == len(
                        body.encode("utf-8")
                    )

                    # Step 2: read it back and confirm the
                    # body landed verbatim. This is the
                    # create-then-read round trip the T32
                    # ticket's "Done when" calls for.
                    read_back = await session.call_tool(
                        "read_page", {"name": T32_MARKER}
                    )
                    assert read_back.is_error is False, read_back
                    read_payload = read_back.structured_content or {}
                    assert read_payload.get("body") == body
    finally:
        server_task.cancel()
        with suppress(asyncio.CancelledError):
            await server_task
        await _delete_t32_marker(sb_url, sb_token)


# ---------------------------------------------------------------------------
# T33: prepend_to_page end-to-end against live SB.
#
# The T33 ticket's "Done when" calls for a T7-style live
# test that prepends to a journal page with a YAML frontmatter
# and verifies the frontmatter is still at the top and the
# new content is just below. This test exercises that round
# trip end-to-end against the dev-box SB.
# ---------------------------------------------------------------------------

T33_MARKER = "e2e-mcp-silverbullet-t33-prepend.md"


async def _delete_t33_marker(sb_url: str, sb_token: str) -> None:
    headers: dict[str, str] = {}
    if sb_token:
        headers["Authorization"] = f"Bearer {sb_token}"
    async with httpx.AsyncClient(base_url=sb_url, headers=headers, timeout=5.0) as client:
        with suppress(httpx.HTTPError):
            await client.delete(f"/.fs/{T33_MARKER}")


@pytest.mark.asyncio
async def test_prepend_to_page_round_trip_with_frontmatter() -> None:
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
        list_pages_hydrate_etags=False,
    )
    # Pre-populate the page via the bridge so the test
    # doesn't depend on raw ``/.fs`` PUT semantics outside
    # the bridge surface. ``create_page`` (T32) is the
    # right tool for the setup write.
    original_body = "---\ntitle: T33 marker\ntags: [test]\n---\noriginal body line\n"
    prepend_content = "PREPENDED LINE\n"

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

                    # Step 1: create the marker with a YAML
                    # frontmatter and a body line. Use
                    # ``create_page`` so we don't have to
                    # reason about whether ``If-Match: *``
                    # is honored on this dev box's SB.
                    created = await session.call_tool(
                        "create_page",
                        {"name": T33_MARKER, "content": original_body},
                    )
                    assert created.is_error is False, created

                    # Step 2: prepend with the default
                    # ``position="after_frontmatter"``.
                    # The new content should land *between*
                    # the closing ``---`` and the first body
                    # line — the human-meaningful default for
                    # journal / daily-notes pages with YAML
                    # frontmatter.
                    prepended = await session.call_tool(
                        "prepend_to_page",
                        {
                            "name": T33_MARKER,
                            "content": prepend_content,
                        },
                    )
                    assert prepended.is_error is False, prepended

                    # Step 3: read it back and verify the
                    # frontmatter is still at the top AND
                    # the new content is just below.
                    read_back = await session.call_tool(
                        "read_page", {"name": T33_MARKER}
                    )
                    assert read_back.is_error is False, read_back
                    body = (read_back.structured_content or {}).get("body", "")
                    expected = (
                        "---\n"
                        "title: T33 marker\n"
                        "tags: [test]\n"
                        "---\n"
                        "PREPENDED LINE\n"
                        "original body line\n"
                    )
                    assert body == expected, (
                        f"T33: prepended body should land "
                        f"below frontmatter; got:\n{body!r}\n"
                        f"expected:\n{expected!r}"
                    )
    finally:
        server_task.cancel()
        with suppress(asyncio.CancelledError):
            await server_task
        await _delete_t33_marker(sb_url, sb_token)


# ---------------------------------------------------------------------------
# T36: body-size cap end-to-end against live SB.
#
# The T36 ticket's "Done when" calls for Layer-1 + Layer-2
# coverage; live-SB integration is not needed because the
# cap is enforced locally (before the SB round trip) and the
# failure surface is purely local. This is a sanity-check
# live test that exercises the cap on ``write_page`` and
# confirms the SB PUT *never happens* — a regression that
# moved the cap check below the SB round trip would surface
# here as an actual PUT against the live SB.
# ---------------------------------------------------------------------------

T36_MARKER = "e2e-mcp-silverbullet-t36-cap.md"


async def _delete_t36_marker(sb_url: str, sb_token: str) -> None:
    headers: dict[str, str] = {}
    if sb_token:
        headers["Authorization"] = f"Bearer {sb_token}"
    async with httpx.AsyncClient(base_url=sb_url, headers=headers, timeout=5.0) as client:
        with suppress(httpx.HTTPError):
            await client.delete(f"/.fs/{T36_MARKER}")


@pytest.mark.asyncio
async def test_body_size_cap_fires_before_sb_round_trip() -> None:
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
        list_pages_hydrate_etags=False,
    )
    # 256 KiB + 1 byte of "a" — one byte over the cap.
    over_cap_body = "a" * (256 * 1024 + 1)
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

                    # The cap fires locally — the bridge
                    # raises ``body too large`` before any
                    # SB round trip. ``is_error=True`` with
                    # the ``body too large`` wording.
                    result = await session.call_tool(
                        "write_page",
                        {"name": T36_MARKER, "content": over_cap_body},
                    )
                    assert result.is_error is True
                    assert "body too large" in result.content[0].text
                    # The error names the size (so the agent
                    # can act on it) and the cap.
                    assert str(256 * 1024 + 1) in result.content[0].text
                    assert "256 KiB" in result.content[0].text
                    assert "append_to_page" in result.content[0].text

                    # Verify the page was NOT created on SB
                    # (the cap fired locally, the PUT never
                    # happened). A subsequent ``read_page``
                    # should 404 (or, on this dev box, fail
                    # with the standard ``page not found``
                    # wording) — *not* return the oversized
                    # body, which would prove the cap failed
                    # to fire.
                    read_back = await session.call_tool(
                        "read_page", {"name": T36_MARKER}
                    )
                    assert read_back.is_error is True
                    assert "page not found" in read_back.content[0].text
    finally:
        server_task.cancel()
        with suppress(asyncio.CancelledError):
            await server_task
        # Defensive — the page shouldn't exist (the cap
        # fired before the PUT), but the cleanup is
        # best-effort.
        await _delete_t36_marker(sb_url, sb_token)

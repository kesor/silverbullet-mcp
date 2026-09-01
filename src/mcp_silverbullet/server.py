"""MCP server wiring for the bridge.

Locked at T4 of the prior map (three ``/.fs``-backed tools) and grown
by v1.1: T18 added ``delete_page``, T19 added ``append_to_page``,
T20 added ``patch_page_lines``, T21 added ``patch_page_replace``,
T22 added ``move_page``. v1.2's T23 widens every write tool's
return type from ``str | None`` (the new ETag) to a
:class:`PageMeta` acknowledgement envelope so an agent that just
made a write knows ``size_bytes`` / ``last_modified_ms`` /
``created_ms`` without a follow-up read. T24 widens the read-side
tool shape (``read_page`` and the ``silverbullet://page/{name}``
resource template) to match. T25 adds ``page_exists`` for a
ninth ``/.fs``-backed tool — a cheap ``GET`` that returns
``bool`` so an agent can answer "does this page exist?" without
paying for the full read body. T26 adds a ``dry_run=True`` knob
to the three read-modify-write tools (``append_to_page`` /
``patch_page_lines`` / ``patch_page_replace``) so an agent can
preview a patch without committing; the read still happens and
``if_match=<etag>`` is checked against the read's etag (a stale
etag raises 412-equivalent ``ToolError`` so the caller doesn't
think a doomed write would have succeeded), but no PUT is
issued. T27 adds ``diff_pages`` for a tenth ``/.fs``-backed tool
— a line-based unified diff between two pages or a page and a
literal string (``other_name`` xor ``other_body``), read-only, that
reuses the same ``difflib.unified_diff`` plumbing the T26
``dry_run`` envelope uses for its ``diff`` field. T28 widens
``list_pages`` to return the same envelope family the read/write
tools use
(``list[{name, etag, size_bytes, last_modified_ms, created_ms}]``)
and adds an opt-in per-page etag-hydration fallback driven by
``MCP_SILVERBULLET_LIST_PAGES_HYDRATE_ETAGS`` so an operator who
needs ``if_match`` round-trips from a list call can pay the
N+1 cost of one GET per page. T29 adds ``list_tasks`` for an
eleventh tool — an always-on per-page checkbox enumerator
(``list_tasks(page=name)`` reads the page and returns one entry
per ``- [ ]`` / ``- [x]`` / ``- [X]`` bullet) plus an opt-in
space-walk variant (``list_tasks(page=None, prefix="Daily")``)
that walks the SB space directory directly (gated behind the
journal config the same way ``journal_histogram`` etc. are).
T30 adds ``check_task`` for a twelfth tool — flip a checkbox
bullet's state by its wikilink ref (``check_task(page, ref,
state="done")`` reads the page, finds the unique bullet whose
wikilink target equals ``ref``, flips the marker, and writes
the body back via ``write_page(if_match=<read_etag>)`` so a
concurrent edit fails 412 rather than silently clobbering the
flip). The bridge registers thirteen ``/.fs``-backed tools
(``read_page`` / ``page_exists`` / ``write_page`` /
``create_page`` / ``delete_page`` / ``append_to_page`` /
``prepend_to_page`` / ``patch_page_lines`` / ``patch_page_replace`` /
``move_page`` / ``list_pages`` / ``diff_pages`` / ``check_task``)
plus one bullet enumerator (``list_tasks``) plus one resource
template (``silverbullet://page/{name}``). Each tool closes over a
single :class:`SBClient` opened at boot; SB's typed exceptions
translate to :mcp_exc:`ToolError` with the exact wording from
``docs/design.md`` § Tools § Status-code mapping, all funneled
through :func:`_translate_sb_errors`.

T10 of the v1.1 map adds an optional, gated journal surface
(``journal_histogram`` / ``tag_summary`` / ``recent_pages`` /
``pages_touching_topic``) that reads the SB space directory directly.
The gate is opt-in: ``build_mcp(..., journal=JournalConfig(enabled=True,
space_path=...))`` adds the four journal tools; otherwise the bridge
registers only the thirteen ``/.fs``-backed tools plus one bullet
primitive (``list_tasks``) — fourteen total —
and the resource template. See :mod:`mcp_silverbullet.journal` for
the gate logic.

T34 of v1.3 adds ``search_pages`` to the journal surface — a
bounded wrapper over T12's machinery with a ``limit`` knob
(default 20, hard cap 100). T35 adds ``find_backlinks`` — a
wikilink-target backlink scan over the SB space directory.
T32 adds ``create_page`` — a refuse-to-overwrite create tool
distinct from ``write_page``'s overwrite-or-create default. T33
adds ``prepend_to_page`` — a top-of-body insert with YAML
frontmatter awareness. Together these bring the bridge to fourteen
``/.fs``-backed + bullet-primitive tools, plus the resource
template, and the journal surface to six tools
(T10–T12, T34, T35). The v1.2 build map at
``docs/wayfinder/map-v1.2.md` tracks the agent-facing QOL tickets
(T23/T24/T25/T26/T27/T28/T29/T30 done; destination reached); the
v1.3 build map at ``docs/wayfinder/map-v1.3.md` tracks the
discovery + edit-hygiene tickets (T31/T31a/T31b/T32/T33/T34/T35/
T36 done; destination reached).

See ``docs/design.md`` § Tools for the tool surface, § SilverBullet
client contract for the SB-side status codes, and
``docs/wayfinder/map.md` (v1) / ``docs/wayfinder/map-v1.1.md` (v1.1)
for the T3/T4/T10/T18/T19/T20/T21 decisions this implements.
"""

from __future__ import annotations

import contextlib
import difflib
import json
import time
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import TypeVar

# T47: type variable for ``_auto_retry_on_concurrent_edit``'s
# generic return type. The helper is typed against
# ``Callable[[], Awaitable[T]]`` so the return type flows
# through to the caller without coercion.
_T = TypeVar("_T")
from pathlib import Path

import httpx2 as httpx
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver.exceptions import (
    ResourceError,
    ResourceNotFoundError,
    ToolError,
)
from mcp.server.mcpserver.server import MCPServer

from mcp_silverbullet.journal import (
    JournalConfig,
    _apply_checkbox_flip,
    _find_task_bullet,
    _list_tasks_for_space,
    _parse_tasks,
    _validate_check_task_state,
    register_journal_tools,
)
from mcp_silverbullet.sb_client import (
    BodyTooLarge,
    FileMeta,
    PageMeta,
    PageNotFound,
    PreconditionFailed,
    SBClient,
    SBError,
    ServerError,
)
from mcp_silverbullet.verifier import StaticTokenVerifier
from mcp.server.auth.provider import TokenVerifier


# Where the bridge thinks it lives. ``resource_server_url`` is the
# URL Grok is talking to (drives ``WWW-Authenticate: Bearer
# resource_metadata=…`` and the discovery doc); ``issuer_url`` is what
# shows up under ``authorization_servers`` in the same doc. v1 has no
# separate authz server (T2 of the prior map: static bearer, no
# dance), so the bridge is its own issuer — honest about what we are.
_DEFAULT_RESOURCE_URL = "http://127.0.0.1:8000/mcp"

# Body limit printed verbatim into the 413 ``ToolError``; matches the
# SDK's ``DEFAULT_MAX_REQUEST_BODY_SIZE`` (4 MiB) and
# ``sb_client._BODY_LIMIT_BYTES``. Used by :func:`_translate_sb_errors`
# so the wording is in exactly one place — future tightening of the
# limit (or the SDK default drifting) is a single-line change.
_BODY_LIMIT_MIB = 4


# T42 contention-hint knobs. After ``_CONTENTION_THRESHOLD`` 412s on
# the same page within ``_CONTENTION_WINDOW_SECONDS``, the next 412
# ``ToolError`` carries a ``[concurrent_edit_hint: true]`` marker so
# an agent stuck in a contention loop (the bug reporter's W36
# pattern — SB UI racing a second editor every few minutes) gets a
# clear signal to back off rather than pattern-matching
# ``precondition failed`` strings. Constants are at module scope so
# tuning is a one-line change, not a ticket.
#
# N=3 / M=60s is a starting point; whether the live contention
# pattern matches is empirical (see T42's resolution for the
# follow-up note). The values are documented in ``docs/design.md``
# § Tools § Status-code mapping 412 row.
_CONTENTION_WINDOW_SECONDS = 60
_CONTENTION_THRESHOLD = 3

# Per-page ring buffer of 412 timestamps. ``_contention_hint``
# pushes the current timestamp, evicts entries older than the
# window, and returns whether the buffer crossed the threshold. A
# bounded per-name memory footprint (one ``deque`` per distinct
# ``name``, max length = ``_CONTENTION_THRESHOLD``) — the deque
# never grows past the threshold because every push that's about
# to make it grow also evicts the oldest entry first. Keys are the
# ``name`` parameter threaded into :func:`_translate_sb_errors`;
# an empty ``name`` (e.g. ``list_pages``) is the no-op path so the
# per-page counter doesn't drift on the list call. The bridge is
# single-process / single-user, so no cross-replica or persistence
# concerns apply.
_contention_log: dict[str, deque[float]] = {}


def _contention_hint(name: str) -> bool:
    """Return whether ``name`` is in a 412-contention window.

    T42: pure side-effect-bearing helper called from
    :func:`_translate_sb_errors`'s ``PreconditionFailed`` clause.
    Pushes the current ``time.monotonic()`` timestamp onto
    ``name``'s deque, evicts entries older than
    :data:`_CONTENTION_WINDOW_SECONDS`, and returns ``True`` once
    the deque length has crossed :data:`_CONTENTION_THRESHOLD`.

    Why a wrapper rather than checking the deque length inline at
    the 412 clause: the helper is the single place that knows the
    threshold and the window, and a future change to either (e.g.
    tuning N down to 2, or widening the window to 5 minutes) is a
    one-line edit here rather than chasing the constant through
    every tool handler.

    Why ``time.monotonic()`` rather than ``time.time()``: the
    helper is wall-clock-agnostic on purpose — system clock
    changes (NTP corrections, DST jumps, container time-skew)
    can't artificially trip the hint. The downside (no
    human-readable timestamps in logs) doesn't apply because the
    helper produces no log output.

    Empty ``name`` is a no-op that returns ``False``: the helper
    still has to be called from :func:`_translate_sb_errors` for
    the non-page tools (``list_pages``), but the per-page counter
    shouldn't drift on those calls. The caller-side guard
    (``if name``) keeps the dict from accumulating empty-string
    keys.
    """
    if not name:
        return False
    log = _contention_log.setdefault(name, deque(maxlen=_CONTENTION_THRESHOLD))
    now = time.monotonic()
    # Evict entries older than the sliding window. The deque is
    # bounded to ``_CONTENTION_THRESHOLD`` entries, so this loop
    # runs at most that many times — no unbounded scan.
    cutoff = now - _CONTENTION_WINDOW_SECONDS
    while log and log[0] < cutoff:
        log.popleft()
    # Trip-on-next semantics: capture the length *before* this push.
    # After N=3 412s land within the window, the deque is at the
    # threshold, and the *next* push (the 4th 412) is the one that
    # carries the hint. ``len(log) >= _CONTENTION_THRESHOLD`` means
    # "this name already burned through the threshold; back off."
    already_at_threshold = len(log) >= _CONTENTION_THRESHOLD
    log.append(now)
    return already_at_threshold


# T47: substring the post-write verification helper's error
# text uses. ``_CONCURRENT_EDIT_MSG`` opens with ``concurrent
# edit detected`` (T45 byte-preservation of the v1.5 prefix);
# ``_translate_sb_errors`` opens with ``precondition failed``.
# We match on the bare prefix to surface ``concurrent edit
# detected`` for auto-retry and let ``precondition failed``
# (the standard-412 path) flow through unchanged — the
# latter means the caller passed an explicit stale ``if_match``
# and SB honored it, which is a precondition failure the agent
# should see, not a race the bridge should mask.
_CONCURRENT_EDIT_PREFIX = "concurrent edit detected"


async def _auto_retry_on_concurrent_edit(
    operation: Callable[[], Awaitable[_T]],
    *,
    max_retries: int,
) -> _T:
    """Auto-retry ``operation`` on T31b silent-overwrite 412.

    T47: when the post-write verification helper
    (:func:`_verify_concurrency_token`) fires
    ``ToolError("concurrent edit detected: …")`` — a real
    concurrent writer touched the page between the bridge's
    PUT and the verification GET — a read-modify-write tool
    can recover by re-reading the body, re-deriving the
    operation with the new etag, and re-PUTing. The
    re-derivation is the *whole point* of this helper: the
    ``operation`` closure captures the tool's inputs (name,
    text, find / new_string, etc.) and re-runs the entire
    read-modify-write block on each iteration, which means
    each retry sees the body's current state.

    Why a closure rather than a decorator: the tools are
    already wrapped by ``@mcp.tool(...)``; adding another
    decorator layer would obscure the tool's parameter
    surface and the ``mcp.tool`` decorator's introspection.
    A closure threaded into ``_auto_retry_on_concurrent_edit``
    keeps the tool handler's body in one place.

    Why match on the bare ``concurrent edit detected`` prefix
    rather than the full ``_CONCURRENT_EDIT_MSG``: T45
    widened the wording (added ``on {name}: …`` and the
    ``{name}``, ``{expected_etag}``, ``{current_etag}``
    placeholders), but kept the bare prefix byte-preserved so
    agents that pattern-match on it still match. The auto-
    retry's substring check rides on the same prefix
    invariant — a future wording tweak that keeps the prefix
    continues to work; a future wording tweak that drops
    the prefix breaks the helper, which is the right
    failure mode (silent overwrite detection is
    load-bearing; the helper should fail loudly if its
    trigger changes).

    Why only ``concurrent edit detected`` and not other
    ``ToolError`` shapes:

    - ``find not found in body`` (``patch_page_replace``) /
      ``line range … out of bounds`` (``patch_page_lines``)
      / ``no task with ref`` (``check_task``) / etc. are
      semantic failures the agent needs to see. The bridge
      doesn't guess the agent's intent on a drifted body —
      a re-find would either succeed (the agent's anchor
      is still there in the new body) or fail with the
      same wording, and the agent should re-read manually
      either way.
    - ``page not found`` means the page was deleted out-of-
      band. Retrying won't help (the page is gone); the
      agent should see the 404 and decide whether to
      re-create or move on.
    - ``precondition failed`` is the standard-412 path
      (SB honored ``If-Match`` and returned 412). The
      agent passed an explicit stale ``if_match``; retrying
      would mask the precondition failure. This helper
      doesn't catch it.
    - ``body too large`` is a static size check (T36)
      that doesn't depend on etag state. Retrying is
      pointless.

    Why ``max_retries=0`` opts out: an agent that needs
    to surface every error (e.g., a test that asserts the
    412 fires) can pass ``0`` and get the pre-T47 behavior.

    The ``operation`` parameter is typed as
    ``Callable[[], Awaitable[_T]]`` so the helper is generic
    over the tool's return type (all read-modify-write tools
    return ``dict[str, object]`` — the T23 ack envelope —
    but the helper doesn't constrain this).

    Bounded backoff is deliberately omitted: each retry
    re-reads the page and re-PUTs synchronously. Adding a
    sleep between retries would help on a sustained writer
    that's faster than the bridge's loop, but the design
    surface (``asyncio.sleep(0.1)``? ``asyncio.sleep(0)``?
    exponential backoff?) is its own ticket.
    """
    last_exc: ToolError | None = None
    for _ in range(max_retries + 1):
        try:
            return await operation()
        except ToolError as exc:
            text = str(exc)
            if not text.startswith(_CONCURRENT_EDIT_PREFIX):
                # Not a post-write-verification race —
                # surface the error unchanged so the agent
                # sees ``find not found``, ``page not
                # found``, ``precondition failed``, body-
                # size errors, etc. without the bridge
                # silently retrying them.
                raise
            last_exc = exc
            # Loop continues; on the next iteration,
            # ``operation`` is invoked again, which (for
            # read-modify-write tools) re-reads the body
            # and re-derives the patched body from the
            # *current* page state.
    # Exhausted. The final ``ToolError`` carries the
    # post-exhaustion verification-GET etag as
    # ``current_etag``; an agent that wants to do its own
    # re-read-and-retry can read that token and try again.
    assert last_exc is not None  # unreachable: the loop
    # either returns (success), raises (non-race error),
    # or falls through (exhausted retry budget with at least
    # one ``concurrent edit detected`` raised).
    raise last_exc


@contextlib.asynccontextmanager
async def _translate_sb_errors(name: str) -> AsyncIterator[None]:
    """Wrap a single ``sb_client`` call, mapping its exceptions to ``ToolError``.

    Every tool handler (``read_page``, ``write_page``, ``delete_page``,
    ``list_pages``, ``diff_pages``, ``append_to_page``,
    ``patch_page_lines``, ``patch_page_replace``, ``move_page``)
    closes over the same :class:`SBClient` and surfaces the same
    five exception types with the same wording from
    ``docs/design.md`` § Tools § Status-code mapping. Factoring the
    translation into this async context manager keeps the wording
    in one place — a future tightening of a code path (e.g.
    adding ``403`` → ``ToolError("forbidden")``) is a single-line
    change. ``move_page`` (T22) wraps *part* of its
    read-write-delete dance in this helper (the read and the
    destination write) and translates the post-write-delete step
    inline so the caller sees the atomicity-caveat wording ("moved
    body to {new} but failed to delete {old}; both now exist")
    instead of the unified 412 message.

    The 404 wording needs ``name`` (the page the caller asked for)
    rather than the URL the SB request hit — callers care about
    *which* page was missing, not the request's full URL. Tools
    that target a single page (``read_page``, ``write_page``,
    ``delete_page``, ``append_to_page``, ``patch_page_lines``,
    ``patch_page_replace``, ``move_page``, ``page_exists``) pass
    ``name``; ``list_pages`` passes an empty string (and doesn't
    actually raise ``PageNotFound`` on its current code path, so
    the wording never surfaces there). ``diff_pages`` (T27) is the
    compound case — it has *two* ``_translate_sb_errors`` blocks
    (one per read), each keyed on whichever page that read
    targeted (``name`` for the first, ``other_name`` for the
    second), so a 404 on either side surfaces as
    ``ToolError("page not found: <that page's name>")`` and the
    agent can tell which side failed from the wording's ``name``
    field without inspecting the call.

    Python's ``except`` only catches exceptions actually raised in
    the wrapped block, so it's safe to list all five clauses on
    every handler: ``read_page`` (a GET) won't raise
    ``PreconditionFailed`` or ``BodyTooLarge``, so those clauses
    never fire there; ``list_pages`` likewise.

    T42: the ``PreconditionFailed`` clause appends a
    ``[concurrent_edit_hint: true]`` marker to the standard
    412 wording when this page has hit the contention
    threshold (see :func:`_contention_hint`). The marker
    rides on the existing message-text channel because the
    MCP SDK renders ``ToolError`` to the wire as plain
    ``TextContent(text=str(exc))`` — no native envelope
    field. ``create_page`` (T32) intercepts
    ``PreconditionFailed`` *before* this helper, so the
    hint never reaches the ``already exists`` wording.

    T43: the ``ServerError`` clause appends a
    ``[cf_hint: {...}]`` marker carrying the parsed
    ``retry_after`` / ``error_code`` / ``title`` from a
    Cloudflare-shaped 5xx body when
    :attr:`ServerError.cf_hint` was populated by
    :func:`mcp_silverbullet.sb_client._raise_for_status`.
    Same message-text-channel pattern as T42; the
    marker is conditional (``None`` ``cf_hint`` leaves
    the error envelope unchanged), so a non-CF 5xx
    (the common case) sees the pre-T43 wording
    byte-for-byte. An agent that
    ``json.loads`` the marker gets a clean dict to
    decide whether to retry (matching ``retry_after``)
    rather than pattern-matching the raw CF JSON.
    """
    try:
        yield
    except PageNotFound as exc:
        raise ToolError(f"page not found: {name}") from exc
    except PreconditionFailed as exc:
        # T42: surface the contention hint when this page has hit
        # 412 N times within the sliding window. The hint is
        # appended to the standard 412 ``ToolError`` message as a
        # machine-parseable suffix (`` [concurrent_edit_hint: true]``)
        # rather than as a structured envelope field, because the
        # MCP SDK's ``ToolError`` is rendered to the wire as plain
        # ``TextContent(text=str(exc))`` (``mcp/server/mcpserver/
        # server.py:_handle_call_tool``) — no native envelope
        # field exists. An agent that pattern-matches on the
        # standard ``precondition failed`` wording still matches;
        # an agent that knows the new marker can extract it. The
        # marker only appears when the threshold trips, so a
        # one-off 412 (the common case) is unchanged.
        #
        # T45: enrich the standard 412 wording with a literal
        # ``read_page(<name>)`` pointer (the resolved page name
        # the tool targeted) when the bridge knows the page —
        # every per-page tool passes ``name`` to this helper;
        # ``list_pages`` passes ``""`` and is treated as a
        # non-page-scoped precondition (no pointer surfaces).
        # The pointer sits between the T42-style "precondition
        # failed; check if_match/if_none_match" prefix and the
        # T42 ``[concurrent_edit_hint: true]`` suffix (when the
        # contention window trips), so an agent that
        # pattern-matches on either the bare prefix or the
        # marker substring still matches. The standard path can't
        # embed the current etag directly — SB's 412 response
        # body is empty on this build (per the live SB the
        # bridge tested against) — so the wording points the
        # agent at ``read_page(name)``, which will return the
        # current etag. The silent-overwrite 412
        # (:func:`_verify_concurrency_token`) is the path where
        # the bridge has the fresh etag in hand and the wording
        # embeds it directly; this helper only sees the standard
        # path.
        msg = "precondition failed; check if_match/if_none_match"
        # T45: when the bridge knows the page that was preconditioned
        # (every tool except ``list_pages``, which passes an empty
        # string), surface a literal ``read_page(<name>)`` pointer so
        # the agent doesn't have to guess the read tool's name or
        # thread the page name itself. ``list_pages`` is the
        # boundary case — its 412 isn't a per-page precondition (the
        # call has no ``if_match`` surface), so an empty ``name``
        # here means "the precondition wasn't page-scoped; just
        # re-issue the call." Skipping the pointer for empty names
        # keeps the message coherent in that edge case.
        if name:
            msg += f'; read_page("{name}") for the current etag and re-issue'
        if _contention_hint(name):
            msg += " [concurrent_edit_hint: true]"
        raise ToolError(msg) from exc
    except BodyTooLarge as exc:
        raise ToolError(f"body too large: limit is {_BODY_LIMIT_MIB} MiB") from exc
    except ServerError as exc:
        # T43: surface the CF hint when the 5xx body looked
        # CF-shaped (parsed upstream in :func:`_parse_cf_error`).
        # Same message-text-channel pattern as T42's
        # ``concurrent_edit_hint``: the MCP SDK renders ``ToolError``
        # to the wire as plain ``TextContent(text=str(exc))`` (no
        # native envelope field), so the hint rides as a
        # machine-parseable suffix on the standard wording. An
        # agent that pattern-matches on the standard wording still
        # matches; an agent that knows the new marker can extract
        # it and back off based on ``retry_after``. The marker is
        # **conditional** — only present when ``exc.cf_hint`` is
        # populated — so a non-CF 5xx (the common case: SB behind a
        # plain reverse proxy) sees the unchanged wording, byte-
        # for-byte. The marker uses ``json.dumps`` (not
        # ``repr``) so the suffix is a clean JSON object the
        # agent can ``json.loads`` directly: `` [cf_hint:
        # {"retry_after": 60, "error_code": 502, "title": "Error
        # 502: Bad gateway"}]``.
        msg = str(exc)
        if getattr(exc, "cf_hint", None):
            msg += f" [cf_hint: {json.dumps(exc.cf_hint)}]"
        raise ToolError(msg) from exc
    except httpx.TimeoutException as exc:
        raise ToolError("silverbullet request timed out") from exc


# Wording printed verbatim into the T31b concurrency-conflict error.
# Centralized here so future tweaks (renaming the conflict class,
# adding the offending etag to the message) don't ripple across the
# six call sites that thread an etag through ``if_match``.
#
# T45: two new placeholders — ``{name}`` (the resolved page name
# the write targeted) and ``{current_etag}`` (the post-write etag
# the helper just synthesized from the verification re-read).
# The agent that followed the concurrency protocol correctly sees
# the bridge tell it the exact ``if_match=`` value for the next
# call, without an extra read round trip — the bridge just did
# the read for them. Wording is byte-additive over the v1.3 /
# v1.4 / v1.5 message: the existing prefix ("concurrent edit
# detected: …") and the trailing "current etag" clause stay
# verbatim; only the "on {name}" anchor and the literal
# "if_match={current_etag}" copy-paste hint are new. An agent
# that pattern-matches on the original "concurrent edit
# detected" substring still matches; an agent that pinned the
# byte-for-byte full message (a small set of T31b tests) needs
# to update.
_CONCURRENT_EDIT_MSG = (
    "concurrent edit detected on {name}: the page changed since we "
    "wrote at {expected_etag}; current etag is {current_etag} — "
    "re-issue the write with if_match={current_etag}"
)


# Body-size cap applied uniformly across every write tool
# (T36). 256 KiB matches ``xmatthewx/silverbullet-mcp-server``'s
# cap (the closest v1.3 competitive-landscape reference for the
# feature). Small enough to surface clearly to an agent as "you
# tried to write 600 KB to a journal page, that won't fit, here's
# the remediation hint"; large enough to be a non-issue for any
# human-authored page. The cap is enforced *before* the SB
# round trip on every write tool, so the read step on
# read-modify-write tools is unaffected (a 500 KB existing
# page is fine; a 500 KB about-to-be-written payload is not).
_BODY_SIZE_CAP_BYTES = 256 * 1024


def _check_body_size(body: str) -> None:
    """Raise ``ToolError("body too large: …")`` when the body exceeds the 256 KiB cap.

    T36: every write tool (``write_page``, ``create_page``,
    ``prepend_to_page``, ``append_to_page``,
    ``patch_page_lines``, ``patch_page_replace``,
    ``move_page``, ``check_task``) calls this helper at the
    top of its handler, before any SB round trip. The
    measurement is on the UTF-8 byte count of the
    *caller-supplied* body — ``write_page``'s ``content``,
    ``append_to_page``'s ``text``, ``prepend_to_page``'s
    ``content``, ``patch_page_lines``'s ``new_content``,
    ``patch_page_replace``'s ``new_string``, ``move_page``'s
    whole-page body. This matches the spirit of the
    T36 ticket's "you tried to write 600 KB to a journal
    page, that won't fit" framing and avoids the
    surprise of ``append_to_page(name, text="100KB")``
    against a 200 KB existing page silently working but
    ``text="200KB"`` against the same page hitting the cap
    due to post-shaping concatenation.

    The cap is measured on the body the bridge is *about
    to write* — not the request body's Content-Length
    (the bridge might be re-encoding the body), not the
    body the bridge just read on read-modify-write tools
    (a 500 KB existing page is fine; a 500 KB
    about-to-be-written payload is not), and not the
    page's stored ``size_bytes`` (which is the size
    *after* a prior write, not the size of the current
    request).

    The 256 KiB cap is inclusive — a body of *exactly*
    256 KiB passes the check, a body of 256 KiB + 1 byte
    fails. The boundary case is covered by a dedicated
    test (`test_check_body_size_accepts_exact_cap`).

    The error message names both the body size and the cap
    (so the agent sees the numbers, not just a vague
    "too large"), and includes a remediation hint that
    names the right next tool (``append_to_page`` chunks)
    — same pattern the rest of the bridge's error
    wording uses (clear next-step hint, not a vague
    "failed").

    Out of scope (per the T36 ticket):
    - Configurable cap (`MCP_SILVERBULLET_BODY_SIZE_CAP`).
      ``xmatthewx``'s cap is fixed; we mirror that.
      Operators who need a different cap can fork.
    - The cap does NOT replace SB's own size limits. SB
      may accept a smaller body than the cap; the
      bridge's cap is a guardrail for the agent, not a
      promise about SB's limits.
    - Streamed / chunked writes. The cap is the boundary
      that tells an agent "stop trying to write 600 KB
      in one call; use ``append_to_page`` chunks". A
      streamed / chunked write tool is a separate
      design effort and a v1.4+ concern.
    """
    size_bytes = len(body.encode("utf-8"))
    if size_bytes > _BODY_SIZE_CAP_BYTES:
        raise ToolError(
            f"body too large: {size_bytes} bytes exceeds "
            f"{_BODY_SIZE_CAP_BYTES} byte (256 KiB) cap; "
            f"chunk into append_to_page calls"
        )


def _validate_nonempty_name(name: str) -> None:
    """Raise ``ToolError("name must not be empty")`` when ``name`` is blank.

    T40: lifts the upfront empty-name guard that
    :func:`create_page` already shipped as an inline check, into a
    module-scope helper threaded into every ``name``-taking write
    tool (``write_page``, ``delete_page``, ``move_page``'s source
    and destination, ``patch_page_lines``, ``patch_page_replace``,
    ``check_task`'s ``page``). One helper, one shape, one message.

    Rejects both empty (``""``) and whitespace-only (e.g.
    ``"   "``, ``"\\n"``) names — SB would reject them downstream
    with a less-helpful 500, and the agent needs to see the bug
    pinned at the call site. Mirrors :func:`_validate_nonempty_value`
    for body-shaped inputs.

    The guard fires *before* :func:`_normalize_page_name` so a
    caller passing ``name=""`` still sees the loud
    ``ToolError("name must not be empty")`` rather than the
    normalized form ``".md"`` silently succeeding. Order matters:
    empty guards fire on the caller's raw input; normalization
    fires on the validated input.

    Wording is fixed (``"name must not be empty"``) — matches
    the existing inline guard on :func:`create_page` so agents
    that have learned the shape for one tool see the same shape
    across all of them. The helper has no caller-supplied label
    because ``name`` is always the parameter; if a future tool
    takes a *different* shape (e.g. ``source_path``), call
    :func:`_validate_nonempty_value` with a label instead.
    """
    if not name or not name.strip():
        raise ToolError("name must not be empty")


def _validate_nonempty_value(value: str, *, label: str) -> None:
    """Raise ``ToolError("<label> must not be empty")`` when ``value`` is blank.

    T40: parameterized sibling of :func:`_validate_nonempty_name`
    for body-shaped inputs whose parameter name varies by tool:
    ``content`` (``write_page`` / ``prepend_to_page``),
    ``text`` (``append_to_page``), ``find``
    (``patch_page_replace``), ``ref`` (``check_task``'s wikilink
    target).

    Same rejection rules as :func:`_validate_nonempty_name`:
    empty (``""``) and whitespace-only (``"   "``, ``"\\n"``)
    values both raise. ``label`` is the exact parameter name as
    the agent wrote it (``"content"``, ``"text"``, ``"find"``,
    ``"ref"``) so the error message reads naturally to the
    caller:

    - ``write_page(name="x", content="")`` →
      ``ToolError("content must not be empty")``
    - ``append_to_page(name="x", text="")`` →
      ``ToolError("text must not be empty")``
    - ``prepend_to_page(name="x", content="")`` →
      ``ToolError("content must not be empty")``
    - ``patch_page_replace(name="x", find="")`` →
      ``ToolError("find must not be empty")``
    - ``check_task(page="x", ref="")`` →
      ``ToolError("ref must not be empty")``

    The helper has no implicit default for ``label`` because a
    wrong label produces a wrong-looking error message — the
    caller must pass it. This matches the standing-preferences
    rule "lift the existing guard, don't invent a new shape":
    every existing inline guard named the parameter correctly,
    so the threaded-in version does too.

    **Not** threaded for ``patch_page_replace``'s ``new_string``:
    empty ``new_string`` is the documented "delete every match"
    path (``"abcdefg".replace("cd", "")`` is ``"abefg"``), not a
    caller bug. The ticket originally proposed guarding it too;
    the documented delete-match surface takes priority.
    """
    if not value or not value.strip():
        raise ToolError(f"{label} must not be empty")


def _normalize_page_name(name: str) -> str:
    """Resolve a caller-supplied page name to SB's canonical form.

    T39: SB stores pages as ``*.md`` on disk and the ``/.fs`` HTTP
    API keys by the exact name the caller passes. So an agent that
    calls ``read_page("Foo")`` would 500 (because ``/.fs/Foo``
    doesn't exist), where ``read_page("Foo.md")`` returns the
    body. The split is invisible to humans (SB's editor hides the
    suffix) and a recurring trip-hazard for agents.

    The helper applies two rules:

    - **Strip whitespace** around the name. A caller passing
      ``"  Foo  "`` sees the same page as ``"Foo"``; the strip
      matches the defensive stance :func:`mcp_silverbullet.journal
      ._normalize_link_target` takes on wikilink targets.
    - **Append ``.md``** when the *basename* has no ``.`` at
      all. ``Foo`` → ``Foo.md``; ``Projects/Foo`` →
      ``Projects/Foo.md``. Names with at least one ``.`` in
      the basename pass through unchanged (``Foo.txt`` stays
      ``Foo.txt``; ``Foo.tar.gz`` stays ``Foo.tar.gz``;
      ``.gitignore`` stays ``.gitignore``). SB doesn't store
      multi-extension files, so the rule is mostly defensive
      against future extensions — the only case the agent
      routinely sees is ``*.md``, which the helper handles
      correctly as a no-op (already has a ``.``).

    The helper is **idempotent** (calling it twice yields the
    same value — the second call's input already has a ``.``,
    so the append branch is a no-op), **pure** (no log output,
    no metrics, no observable side effect), and
    **non-validating**: an empty input is *not* rejected here.
    T40's empty-input guard runs *before* this helper at every
    call site, so a caller passing ``name=""`` still sees
    ``ToolError("name must not be empty")`` rather than the
    normalized form ``".md"`` silently succeeding. The helper
    itself returns ``""`` for empty input — defense-in-depth
    for the rare path that bypasses T40 (no such path exists
    today; documented for the future).

    Threading: every tool handler that accepts a ``name``
    parameter (``read_page``, ``page_exists``, ``write_page``,
    ``create_page``, ``delete_page``, ``append_to_page``,
    ``prepend_to_page``, ``patch_page_lines``,
    ``patch_page_replace``, ``move_page``'s ``name`` and
    ``new_name``, ``diff_pages``'s ``name`` and ``other_name``,
    ``check_task``'s ``page``, ``list_tasks``'s ``page``, and
    the ``silverbullet://page/{name}`` resource template)
    calls this helper at the top of the handler, before any
    SB round trip and before :func:`_check_body_size`. The
    ``check_task`` ``ref`` argument is *not* normalized — it's
    a wikilink target, not a page name, and the existing
    :func:`mcp_silverbullet.journal._normalize_link_target`
    handles wikilink canonicalization (in the *strip* direction,
    not the *add* direction).
    """
    stripped = name.strip()
    if "." in stripped.rsplit("/", 1)[-1]:
        return stripped
    return stripped + ".md"


def _name_resolution_payload(
    requested: str, resolved: str
) -> dict[str, object]:
    """Build the T39 feedback-loop envelope when the caller's name was normalized.

    The helper returns an empty dict when ``requested == resolved``
    (the caller's input was already canonical; no extra field
    surfaces, and existing wire-shape assertions on the success
    envelope continue to pass byte-for-byte). When the names
    differ, the helper returns
    ``{"name_resolution": {"requested": …, "resolved": …, "suffix_added": …}}``
    so the agent sees *exactly* what the bridge changed and can
    learn the convention for its next call.

    ``suffix_added`` is ``".md"`` when the bridge appended the
    canonical markdown extension; ``None`` when the helper only
    stripped whitespace (a caller passing ``"  Foo.md  "`` sees
    the whitespace-stripped name but ``suffix_added`` is
    ``None`` — the bridge didn't add a suffix). The split lets
    an agent distinguish "I forgot the extension" (the common
    case T39 was chartered for) from "I had stray whitespace"
    (less common, but still surfaces in the envelope).
    """
    if requested == resolved:
        return {}
    stripped_requested = requested.strip()
    suffix_added = ".md" if resolved == stripped_requested + ".md" else None
    return {
        "name_resolution": {
            "requested": requested,
            "resolved": resolved,
            "suffix_added": suffix_added,
        }
    }


async def _verify_concurrency_token(
    sb_client: SBClient,
    name: str,
    *,
    post_write_meta: PageMeta,
    expected_etag: str | None,
    dry_run: bool = False,
) -> None:
    """Post-write concurrency-token check for SBs that don't honor ``If-Match``.

    T31's live verification surfaced the v1.3-blocking SB fact:
    this dev-box SB returns 200 on a stale ``If-Match`` instead of
    412, so the v1.2 / v1.3 ``If-Match`` contract is not enforced
    server-side. Without a check, an agent that does
    ``read → write(if_match=read_etag)`` on this SB silently
    overwrites a page a concurrent agent already updated.

    The fix is post-write: re-read the page after a 200 write and
    compare the post-write etag against the etag the PUT *response*
    said the page is at (``post_write_meta.etag``). A mismatch
    means a concurrent writer touched the page between our PUT and
    the verification GET — the very race ``If-Match`` exists to
    prevent. The helper raises :exc:`ToolError("concurrent edit
    detected: …")` so the agent sees the same conflict signal it
    would have seen on an SB that honored ``If-Match``, just
    delivered later in the round trip.

    T46: the comparison's reference point changed from
    ``expected_etag`` (the caller's pre-write ``if_match``, which
    is also the auto-threaded read etag for read-modify-write
    tools) to ``post_write_meta.etag`` (the bridge's view of
    "what we just wrote", from the PUT response). Pre-T46 the
    helper compared the verification GET's etag against the
    caller's pre-write etag — which *correctly* detects a race on
    SBs that emit a real ``ETag`` (the etag identifies the resource
    version; a mismatch means someone else wrote), but
    *incorrectly* fires on every read-modify-write that grows the
    page on the synthesized-etag path (the synthesized form is
    ``str(size_bytes)`` per T44, and the post-write size always
    differs from the pre-write size when the bridge writes a body
    that grew). Live reproduction on this dev box: 76 spurious
    "concurrent edit detected" errors in 6 hours on
    ``Trading Book/Logs/2026-W36.md``, every one of them with
    ``current_etag - expected_etag`` exactly equal to the size of
    the appended content. The T46 fix is structural — the bridge
    now asks "is the resource still at the version the PUT
    response said it was?" rather than "is the resource still at
    the version the caller read at?". On read-modify-write tools
    the pre-write-read etag is no longer relevant to the
    comparison — it remains in the ``expected_etag`` parameter
    and the error wording for forensics (an agent that wants to
    know "what did the bridge just write" reads
    ``expected_etag``; an agent that wants the next-call etag
    reads ``current_etag``).

    T45: the raised ``ToolError`` widens ``_CONCURRENT_EDIT_MSG``
    from the v1.3 / v1.4 / v1.5 single-placeholder form
    (``{expected_etag}``) to a three-placeholder form
    (``{name}``, ``{expected_etag}``, ``current_etag``). The
    bridge has the post-write etag in hand from this very
    re-read, so embedding it in the wording closes the
    "agent does an extra read to learn what the bridge already
    knows" gap — the agent sees the literal ``if_match=`` value
    for the next call without an extra round trip. ``name`` is
    the *resolved* page name (the ``resolved_name`` the call
    site threaded in, after T39's normalization) so the wording
    matches the resolved name across the surface; ``current_etag``
    falls back to the string ``"None"`` when SB stripped the
    ``ETag`` header and the synthesized-etag primitive returned
    ``None`` (the rare case; on this dev box the synthesized form
    is ``"{size_bytes}"`` and is always populated). The wording
    is byte-additive over the v1.3 / v1.4 / v1.5 message — an
    agent that pattern-matches on the original
    ``"concurrent edit detected"`` substring still matches.

    The helper runs only on **200 writes** (silent overwrite).
    On SBs that *do* honor ``If-Match``, the existing 412 path in
    :func:`_translate_sb_errors` still wins — cheaper than a
    re-read, fires before the helper runs. The helper is the
    fallback for SBs that don't honor ``If-Match``; both paths
    exist, the cheaper one wins when it works.

    Verification shape per the T31b ticket:

    - ``expected_etag`` is ``None`` (caller passed ``if_match=None``
      / no precondition requested) → no-op. The caller opted out
      of the concurrency primitive; no race to detect.
    - ``expected_etag`` is ``"*"`` (caller passed ``if_match="*"``,
      meaning "require existence") → no-op. ``"*"`` doesn't
      uniquely identify a body; the re-read will return some etag
      and we have nothing to compare against. ``create_page`` (T32)
      is the canonical user of ``if_match="*"`` and never wants
      this helper to fire on it; this branch enforces that.
    - ``expected_etag`` is a concrete string → re-read, compare.
      A different value (real or synthesized via T31a's
      :func:`synthesize_etag`) means the page was mutated
      out-of-band between the caller's read and the write we just
      completed.
    - Re-read returns 404 → no-op (page was deleted out-of-band;
      the write that just succeeded implies it still existed at
      write time, so a follow-up delete is *post-write* — not a
      concurrency violation the caller can recover from by
      re-reading).
    - Re-read returns a transient error (``ServerError`` /
      ``httpx.TimeoutException``) → the verification step
      degrades gracefully (no false-positive error); the
      concurrency primitive is best-effort. The T31b ticket
      documents this as a deliberate tradeoff: the alternative
      is a false-positive "concurrent edit" on a flaky SB that
      would be much harder to debug than a silently-lost
      verification.
    - ``dry_run=True`` → no-op (no write happened to verify).
      Reads still happen on the dry-run path for ``if_match``
      validation (see ``_validate_if_match_on_read``), but the
      T31b helper is about catching races *after* a successful
      write, which dry-runs don't perform.

    Why a separate helper instead of inlining the re-read in each
    tool handler: the read-modify-write tools
    (``append_to_page``, ``patch_page_lines``,
    ``patch_page_replace``, ``move_page``, ``check_task``) all
    thread ``read_page.etag`` into ``if_match=...`` automatically,
    so the post-write verification is the same shape on all six
    tool paths. Centralizing it in one helper keeps the wording
    (``_CONCURRENT_EDIT_MSG``) in one place — a future change to
    the error class or the remediation hint doesn't ripple
    through six call sites.

    Wire shape: a 200 write whose post-write etag differs from
    ``expected_etag`` raises ``ToolError(_CONCURRENT_EDIT_MSG)``
    *before* the T23 ack envelope is constructed, so the agent
    never sees a 200 on a stale write. The 412-equivalent path
    (SB honors ``If-Match``) remains the primary concurrency
    primitive on SBs that support it; this helper is the fallback
    for SBs that don't.
    """
    if dry_run:
        return
    if expected_etag is None or expected_etag == "*":
        return
    # Re-read the page and compare. We deliberately use
    # ``read_page_meta`` (no body) rather than ``read_page`` here:
    # the re-read is a concurrency check, not a body fetch — the
    # body is irrelevant to the etag comparison, and skipping the
    # body keeps the helper cheap on large pages. ``read_page_meta``
    # raises :class:`PageNotFound` if the page vanished (treated as
    # "verification skipped" — see the re-read-failure note in the
    # docstring) and :class:`ServerError` on 5xx (also "skipped",
    # not surfaced as a concurrency error).
    try:
        post_meta = await sb_client.read_page_meta(name)
    except PageNotFound:
        return  # page deleted post-write; not a concurrency violation
    except (ServerError, httpx.TimeoutException):
        return  # transient SB failure; verification is best-effort
    if post_meta.etag != post_write_meta.etag:
        # T46: compare against the PUT *response* etag (the
        # bridge's view of "what we just wrote") rather than
        # against the caller's pre-write etag. The PUT
        # response's etag is what SB told the bridge the
        # resource version is *immediately* after the bridge
        # wrote; the verification GET asks "is the resource
        # still at that version?". A mismatch means a
        # concurrent writer touched the page between the PUT
        # and the GET — the race the ``If-Match`` precondition
        # exists to prevent. Pre-T46 the comparison was against
        # ``expected_etag`` (the caller's pre-write etag); on
        # the real-``ETag`` path that's equivalent (the etag
        # identifies the version), but on the synthesized-etag
        # path (``str(size_bytes)`` per T44) the post-write
        # size always differs from the pre-write size when the
        # bridge writes a body that grew, producing a 100%
        # false-positive rate on every read-modify-write. Live
        # reproduction confirmed: 76 spurious "concurrent edit
        # detected" errors in 6 hours on ``Trading Book/Logs/
        # 2026-W36.md``.
        # T45: format with ``name`` and ``current_etag`` so the
        # agent sees the page the write targeted (the *resolved*
        # name, matching T39's design call — error wording
        # references the resolved name, not the caller's raw
        # input) and the literal ``if_match=`` value for the
        # next call. The bridge has the post-write etag in hand
        # (the verification re-read just synthesized it); the
        # agent doesn't need an extra read round trip to learn
        # it. ``post_meta.etag`` is the etag the bridge surfaces
        # to the tool handler — ``None`` when SB stripped the
        # ``ETag`` header and the synthesized-etag primitive
        # returned ``None``; in that scenario the bridge can't
        # tell the agent the next ``if_match=`` value, so the
        # literal token is ``"None"`` and the agent falls back
        # to the standard path (``read_page(name)``). The
        # ``None`` case is rare on SBs that emit ``ETag`` (the
        # common case); on this dev box the synthesized form is
        # ``"{size_bytes}"`` and is always populated.
        # ``expected_etag`` placeholder carries the PUT-response
        # etag (T46 re-anchoring) — semantically "the page
        # changed since we wrote at" rather than "since you read
        # it at". An agent reading ``expected_etag`` for
        # forensics sees the bridge's view of what it just
        # wrote; an agent reading ``current_etag`` sees the
        # next-call etag.
        current_etag = post_meta.etag
        raise ToolError(
            _CONCURRENT_EDIT_MSG.format(
                name=name,
                expected_etag=post_write_meta.etag,
                current_etag=current_etag,
            )
        )


def _write_meta_to_payload(meta: PageMeta) -> dict[str, object]:
    """Project a :class:`PageMeta` down to the T23 write-tool wire shape.

    T23's wire shape is ``{name, etag, size_bytes, last_modified_ms,
    created_ms}`` (no ``body`` — writes return meta only). The
    underlying :class:`PageMeta` carries the same fields plus an
    optional ``body`` (set on reads; ``None`` on every write); this
    helper subsets it to the v1.2 write-side wire contract so the
    MCP SDK's structured-content serializer doesn't accidentally
    include the body field on write returns. ``body`` is omitted
    rather than serialized as ``None`` so the wire stays clean
    (the SDK doesn't add stray ``None`` keys for unset fields and
    a future migration that drops ``body`` from :class:`PageMeta`
    entirely doesn't leave a dead ``body: null`` field on every
    write response).

    Centralizing this here (rather than inlining ``dataclasses.
    asdict(meta)`` with a manual ``body`` pop in every handler)
    keeps the field subset in one place — :func:`list_pages`
    (T28) reuses the exact same projection for each row (the
    write-shape minus ``body`` is also the list-row shape; one
    helper, two callers), and T29/T30 will route through this
    helper or :func:`_read_meta_to_payload` for any read-modify-
    write step that needs a structured envelope.
    """
    return {
        "name": meta.name,
        "etag": meta.etag,
        "size_bytes": meta.size_bytes,
        "last_modified_ms": meta.last_modified_ms,
        "created_ms": meta.created_ms,
    }


def _read_meta_to_payload(meta: PageMeta) -> dict[str, object]:
    """Project a :class:`PageMeta` down to the T24 read-tool wire shape.

    T24's wire shape is ``{body, etag, size_bytes, last_modified_ms}``
    — the same metadata fields as :func:`_write_meta_to_payload`,
    minus ``name`` (the caller already passed it in; echoing it
    back is noise) and ``created_ms`` (a read has no create-vs-
    update distinction to surface; ``created_ms`` is the page's
    birth time, which doesn't change between reads). ``body`` is
    the only field the write paths populate ``None`` for; on a
    read it's always a string (possibly empty).

    Centralizing this here keeps the field subset in one place —
    the next tickets (T29/T30 add bullet primitives that need a
    read-side envelope) route through this helper. ``body`` is
    materialized as ``""`` (not ``None``) when SB returned an
    empty page, so the wire shape is always ``str`` rather than
    ``str | None`` — MCP clients that read
    ``result.structured_content["body"]`` don't need a None-guard
    for the empty-page case.
    """
    return {
        "body": meta.body or "",
        "etag": meta.etag,
        "size_bytes": meta.size_bytes,
        "last_modified_ms": meta.last_modified_ms,
    }


def _diff_page_envelope(meta: PageMeta) -> dict[str, object]:
    """Project a :class:`PageMeta` to the T27 ``diff_pages`` per-page envelope.

    The T27 wire shape carries ``name`` *as well as* the T24
    read-side fields — the caller of :func:`diff_pages` knows the
    first page's name (they passed it in), but the second page's
    name is hidden behind ``other_name`` and the agent needs to see
    it on the way back to know which page the diff's right side
    came from. Including ``name`` on both envelopes keeps the
    shape parallel (``name`` / ``other`` are sibling objects with
    the same field set) rather than asymmetric (``name`` strips
    ``name``; ``other`` carries it). The extra echo on the first
    page is harmless — the caller already knows the name; the
    wire still has it for log readability.

    Field set: ``{name, body, etag, size_bytes, last_modified_ms}``
    — the T24 read-side envelope with ``name`` re-added.
    ``created_ms`` stays dropped for the same reason as
    :func:`_read_meta_to_payload`: a diff is read-side context,
    and reads have no create-vs-update distinction to surface.
    """
    return {
        "name": meta.name,
        "body": meta.body or "",
        "etag": meta.etag,
        "size_bytes": meta.size_bytes,
        "last_modified_ms": meta.last_modified_ms,
    }


def _split_body_lines(body: str) -> tuple[list[str], bool]:
    """Split ``body`` into editor-shaped lines, plus a trailing-newline flag.

    Returns ``(lines, had_trailing_newline)``. SB stores text as LF
    (no CRLF); we split on ``"\\n"`` directly, not
    :py:meth:`str.splitlines` (which would also normalise ``\\r\\n``
    and friends — and SB never emits those).

    A trailing empty element is dropped: a final ``\\n`` means "the
    last real line ended with a newline", not "there's one more
    empty line after the last real line". The drop makes the line
    count match what an editor displays: ``"a\\nb\\n"`` is two lines
    (``a``, ``b``), not three; ``"a\\nb"`` is also two.

    The ``had_trailing_newline`` flag lets callers (T20's
    :func:`patch_page_lines` in particular) round-trip the file's
    newline-at-end shape: ``"a\\nb\\n"`` → ``(["a", "b"], True)``,
    ``"a\\nb"`` → ``(["a", "b"], False)``. A truly empty body is
    ``([], False)``; a body that's just ``"\\n"`` is ``([""], True)``
    — there's exactly one editor-visible line, which is empty, and
    the file ends with a newline.
    """
    had_trailing_newline = bool(body) and body.endswith("\n")
    lines = body.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines, had_trailing_newline


def _split_frontmatter_block(
    body: str,
) -> tuple[str | None, str]:
    """Split a body into the YAML frontmatter block (with fences) and the rest.

    T33 (``prepend_to_page``) uses this to honor the
    ``position="after_frontmatter"`` default: when a page has
    a leading ``---\n…\n---\n`` block, the prepended content
    goes *between* the closing ``---`` and the first body
    line (not above the opening ``---``). The shape mirrors
    the v1.2 :func:`mcp_silverbullet.journal._split_frontmatter_lines`
    helper but returns the frontmatter as a *single string*
    (with the surrounding fences and trailing newline
    preserved) so the caller can ``frontmatter + content +
    rest_of_body`` without re-joining.

    Returns ``(None, body)`` when the body has no leading
    frontmatter — ``None`` is the canonical "no frontmatter"
    signal (matches the journal helper's contract). A page
    that opens with ``---`` but doesn't close it (a
    malformed frontmatter block) is treated as *no*
    frontmatter: ``(None, body)``. The T33 ticket explicitly
    documents this fallback: ``a page that opens with ``---``
    but doesn't close it (a malformed frontmatter block) is
    treated as no-frontmatter — the new content goes at the
    absolute top, same as a page with no frontmatter.`` This
    is the same "raw text, no parser" pattern the v1.1 /
    v1.2 maps use; we do not pull in a YAML library.

    Wire shape:

    - ``"---\\nfoo: bar\\n---\\nbody\\n"`` →
      ``("---\\nfoo: bar\\n---\\n", "body\\n")``. The trailing
      newline after the closing ``---`` lives in the
      ``frontmatter`` string (so concatenation is correct:
      ``frontmatter + new_content + body``).
    - ``"body\\n"`` (no frontmatter) → ``(None, "body\\n")``.
    - ``"---body\\n"`` (opening fence with no close) →
      ``(None, "---body\\n")``. Malformed → treated as
      no-frontmatter.
    """
    if not body.startswith("---"):
        return None, body
    # ``splitlines`` keeps line boundaries consistent with
    # how the rest of the bridge represents pages (no
    # universal-newline handling — same as the journal
    # helper's contract). ``splitlines`` on the empty
    # string returns ``[]``, which we'd reject with
    # ``len < 2`` below (an opening fence with no body at
    # all is malformed; treat as no-frontmatter).
    lines = body.splitlines()
    if len(lines) < 2:
        return None, body
    # The opening fence must be a *standalone* ``---`` with
    # no leading whitespace (per YAML spec). ``body`` started
    # with ``"---"`` so ``lines[0] == "---"`` (no leading
    # whitespace). Look for the closing fence starting from
    # line index 1 — a standalone ``"---"`` on its own line.
    close_idx: int | None = None
    for idx in range(1, len(lines)):
        if lines[idx] == "---":
            close_idx = idx
            break
    if close_idx is None:
        # Malformed: opening fence but no closing fence.
        # Treat as if no frontmatter was present.
        return None, body
    # Reconstruct the two halves as strings. The
    # ``frontmatter`` string carries the opening fence, the
    # ``…`` lines, the closing fence, AND the trailing
    # newline after the closing fence (if any). Splitlines
    # dropped the newlines, so we rejoin with ``"\n"`` and
    # append the trailing ``"\n"`` iff the original body
    # had one. The body half starts at the line *after*
    # the closing fence, with its trailing newline
    # preserved.
    fm_str = "\n".join(lines[: close_idx + 1])
    # The closing ``---`` was followed by a newline in the
    # original body (every well-formed frontmatter block
    # ends with ``---\n``); preserve that.
    if body[len(fm_str) : len(fm_str) + 1] == "\n":
        fm_str += "\n"
    rest_str = "\n".join(lines[close_idx + 1 :])
    # Restore trailing newline on the body half iff the
    # original body had one (we only added a final newline
    # because splitlines dropped it). The simplest correct
    # rule: if the original body ended with ``"\n"`` and we
    # have a non-empty rest_str, the rest_str should end
    # with ``"\n"``.
    if body.endswith("\n") and rest_str and not rest_str.endswith("\n"):
        rest_str += "\n"
    return fm_str, rest_str


def _apply_line_patch(
    lines: list[str],
    start_line: int,
    end_line: int,
    new_content: str,
) -> str:
    """Replace ``lines[start_line-1:end_line]`` with ``new_content``.

    1-indexed, inclusive: ``start_line=1, end_line=2`` replaces
    ``lines[0:2]``. The replacement is split on ``\\n`` and a
    trailing empty element dropped (same shape as the body), then
    ``\\n``.join'd with the surrounding lines. The result has no
    trailing newline; callers that want to preserve the page's
    trailing newline shape re-attach it themselves (T20 does so
    using the flag :func:`_split_body_lines` returned).
    """
    new_lines = new_content.split("\n") if new_content else []
    if new_lines and new_lines[-1] == "":
        new_lines.pop()
    patched = lines[: start_line - 1] + new_lines + lines[end_line:]
    return "\n".join(patched)


def _validate_if_match_on_read(
    etag: str | None,
    if_match: str | None,
) -> None:
    """Raise 412-equivalent ``ToolError`` if ``if_match`` is a stale etag.

    On the live write path, ``If-Match`` is forwarded to the PUT and
    SB is the source of truth: the read's etag is not consulted (the
    design doc § SilverBullet client contract documents ``If-Match``
    on the PUT row, not the GET row, and the bridge's wire envelope
    threads the *caller's* ``if_match`` arg through verbatim).

    On the T26 dry-run path no PUT happens, so SB never gets to
    enforce the precondition — but the whole point of dry-run is
    "would this write succeed?" If the precondition wouldn't, the
    dry-run envelope should say so before the caller commits. This
    helper is the bridge-side mirror of SB's PUT-side check: if
    ``if_match`` is a concrete etag (not ``"*"``, which means
    "require existence" and is a different shape — it would be
    honoured by the read itself returning 404 if the page were
    missing, not by an etag comparison), and the read's etag
    disagrees, raise the same wording the live path would surface
    when SB returned 412. Centralized so the next patch tool that
    grows ``dry_run`` doesn't have to re-derive the rule.
    """
    if if_match is None or if_match == "*":
        # ``if_match="*"`` is "require existence" — the read path
        # 404s on a missing page, so the precondition is already
        # enforced upstream of this helper. ``None`` means
        # "unconditional" and never raises here.
        return
    if etag != if_match:
        raise ToolError(
            "precondition failed; check if_match/if_none_match"
        )


def _dry_run_payload(original: str, patched: str) -> dict[str, object]:
    """Build the T26 dry-run envelope for a read-modify-write tool.

    Wire shape: ``{dry_run: True, original: str, patched: str, diff: str}``.
    The ``diff`` is a unified diff from :func:`difflib.unified_diff`,
    matching the standing preference "``diff_pages`` is line-based by
    default" in the v1.2 map's Notes — token-level / word-level diff
    is a v1.3 refinement, not v1.2.

    The inputs are split on ``"\\n"`` (not
    :py:meth:`str.splitlines`, which also normalises ``\\r\\n`` and
    friends) to match how SB stores text (LF only). Each output
    line from ``difflib`` gets a single ``"\\n"`` appended so the
    concatenated diff is well-formed: ``lineterm=""`` strips the
    doubled-newline ``difflib`` would otherwise produce, and the
    trailing-newline indicator (``" "`` as the final context line
    when a file lacks a trailing ``\\n``) survives correctly.
    ``difflib.unified_diff`` returns an empty iterator when the two
    inputs are identical, so a no-op patch produces ``diff=""`` —
    the caller reads that as "would have changed nothing" without
    parsing the ``original`` / ``patched`` bodies.
    """
    return {
        "dry_run": True,
        "original": original,
        "patched": patched,
        "diff": "".join(
            line + "\n"
            for line in difflib.unified_diff(
                original.split("\n"),
                patched.split("\n"),
                fromfile="original",
                tofile="patched",
                lineterm="",
            )
        ),
    }


async def _hydrate_list_etags(
    sb_client: SBClient,
    metas: list[PageMeta],
) -> list[PageMeta]:
    """Replace each row's ``etag=None`` with a per-page ``ETag`` header.

    v1.2 T28 fallback: SB's ``GET /.fs`` list payload omits the
    ``etag`` field on this build (the v1 map's T10 decision
    documented this). An operator who needs ``if_match`` round-trips
    from a list call can opt in to per-page hydration via
    ``MCP_SILVERBULLET_LIST_PAGES_HYDRATE_ETAGS=1``; this helper is
    the bridge-side walker that pays the N+1 cost.

    Calls :meth:`SBClient.read_page_meta_safe` for every row that
    has ``etag=None`` on the list payload (rows that already carry
    an etag from a future SB build that emits one are skipped — the
    ``etag is None`` check guards against the double-fetch). The
    "safe" variant swallows per-page failures (404 = deleted
    between list and hydrate; 412 = proxy/SB misconfig; 5xx /
    timeout = transient) and returns ``None``; we then build a new
    :class:`PageMeta` with the hydrated ``etag`` (when present) or
    fall back to the row's original meta (when the hydration
    failed). The list call itself surfaces the full meta either
    way — a single broken page doesn't fail the whole list.

    Walks sequentially rather than concurrently: ``asyncio.gather``
    would let us hydrate N pages in parallel, but ``httpx2`` opens
    a new TCP connection per concurrent request against a loopback
    SB (no keepalive by default), so N concurrent requests against
    a 200-page space would open 200 sockets at once. Sequential is
    slower in wall-clock terms but predictable in resource terms —
    the right shape for a feature that's "off by default, opt-in
    by operators who already know their space size". A future
    ``--max-concurrent`` knob is a v1.3 refinement.

    Returns a *new* list of :class:`PageMeta` with hydrated
    etags; the input list is not mutated. (We don't mutate in
    place because :class:`PageMeta` is a frozen dataclass and
    because mutating the input would surprise callers that hold a
    reference to the same list.)
    """
    out: list[PageMeta] = []
    for meta in metas:
        if meta.etag is not None:
            # A future SB build that emits ``etag`` in the list
            # payload skips the per-page round-trip entirely; the
            # forward-looking shape (a list payload that carries
            # every field) means hydration becomes a no-op when
            # the gap closes.
            out.append(meta)
            continue
        hydrated = await sb_client.read_page_meta_safe(
            _normalize_page_name(meta.name)
        )
        if hydrated is None:
            # Single-page failure (404 / 412 / 5xx / timeout):
            # keep the row's original meta (with ``etag=None``).
            # The list call still returns the page; the agent can
            # ``read_page`` it later if it wants the etag.
            out.append(meta)
            continue
        out.append(
            PageMeta(
                name=meta.name,
                etag=hydrated.etag,
                size_bytes=meta.size_bytes,
                last_modified_ms=hydrated.last_modified_ms,
                created_ms=meta.created_ms,
                body=None,
            )
        )
    return out


def _resolve_verifier(
    verifier: TokenVerifier | None, token: str | None
) -> TokenVerifier:
    """Coalesce the two auth kwargs into a single :class:`TokenVerifier`.

    Precedence: explicit ``verifier`` wins (v1.4 production path,
    set by ``main.build_verifier`` from the operator's env).
    Fallback: construct a :class:`StaticTokenVerifier` from
    ``token`` so v1.x tests (and the ``mcp dev`` CLI session)
    that still pass ``token=...`` keep working.

    Both unset is a programming error: ``build_mcp`` is the only
    caller of this helper and every v1.x / v1.4 call site sets
    one or the other. We raise rather than construct a no-op
    verifier so a misconfigured boot surfaces as an
    ``AttributeError`` at boot time, not as silent
    unauthenticated traffic at request time.
    """
    if verifier is not None:
        return verifier
    if token:
        return StaticTokenVerifier(token)
    raise ValueError(
        "build_mcp requires either `verifier` (v1.4) or "
        "`token` (v1.x compat) to be set; neither was provided"
    )


def build_mcp(
    sb_client: SBClient,
    *,
    verifier: TokenVerifier | None = None,
    token: str | None = None,
    resource_url: str = _DEFAULT_RESOURCE_URL,
    name: str = "mcp-silverbullet",
    journal: JournalConfig | None = None,
    list_pages_hydrate_etags: bool = False,
    log_level: str = "INFO",
) -> MCPServer:
    """Build the configured :class:`MCPServer`.

    Parameters
    ----------
    sb_client
        The outbound ``SBClient`` opened at boot. Held by closure; the
        server doesn't reopen it. v1 has no per-request token refresh,
        so a single client for the process lifetime is correct.
    verifier
        The inbound :class:`TokenVerifier` that validates the
        ``Authorization: Bearer …`` header on each request. v1.4
        accepts either a :class:`JWTVerifier` (default mode;
        validates per-user tokens against the operator's IdP JWKS)
        or a :class:`StaticTokenVerifier` (v1.x compat; compares
        against a single shared secret). Resolved by
        :func:`mcp_silverbullet.main.build_verifier` from the
        operator's ``MCP_SILVERBULLET_AUTH_MODE`` env var; tests
        construct a verifier directly.
    token
        Backwards-compatible alias for ``verifier``: when
        ``verifier`` is unset and ``token`` is set, the bridge
        constructs a :class:`StaticTokenVerifier` from
        ``token`` so v1.x test code (which calls
        ``build_mcp(sb, token="secret")``) keeps working
        unchanged. v1.4 production code should pass
        ``verifier`` explicitly.
    resource_url
        The URL Grok reaches the bridge at. Used for the
        ``WWW-Authenticate`` header and the discovery document. v1
        default is the loopback default; the operator overrides when
        the bridge sits behind a tunnel via the
        ``MCP_SILVERBULLET_RESOURCE_URL`` env var (resolved by
        :func:`mcp_silverbullet.main.load_settings`).
    name
        Server name advertised on the wire.
    journal
        Already-resolved journal gate config. ``None`` means the gate
        is off (no journal tools registered). When the gate is on,
        the bridge registers the six journal-surface tools
        (``journal_histogram`` / ``tag_summary`` / ``recent_pages``
        / ``pages_touching_topic`` / ``search_pages`` / ``find_backlinks``
        — ``search_pages`` is T34, a bounded wrapper over T12 with
        a ``limit`` knob; ``find_backlinks`` is T35, a wikilink-
        target backlink scan) in addition to the fourteen
        ``/.fs``-backed + bullet-primitive tools (``read_page`` /
        ``page_exists`` / ``write_page`` / ``create_page`` /
        ``delete_page`` / ``append_to_page`` / ``prepend_to_page`` /
        ``patch_page_lines`` / ``patch_page_replace`` /
        ``move_page`` / ``list_pages`` / ``diff_pages`` /
        ``check_task`` / ``list_tasks``) and the resource template;
        when off, only the latter are exposed. Resolved by
        :func:`mcp_silverbullet.main.load_settings` from the two
        ``MCP_SILVERBULLET_JOURNAL_*`` env vars; tests construct one
        directly.
    list_pages_hydrate_etags
        v1.2 T28 opt-in: when ``True``, the ``list_pages`` tool
        issues one GET per row to hydrate the etag (SB's list
        payload omits it on this build; an operator who needs an
        ``if_match`` round-trip from a list call pays the N+1 cost
        here). Resolved by
        :func:`mcp_silverbullet.main.load_settings` from
        ``MCP_SILVERBULLET_LIST_PAGES_HYDRATE_ETAGS``; default
        ``False`` (v1.1 wire shape, no per-page round trips).

    The ``AuthSettings`` constructor requires both ``issuer_url`` and
    ``resource_server_url`` to enable the bearer-auth middleware; we
    point both at ``resource_url`` because v1 has no separate authz
    server. The SDK uses these to mount
    ``/.well-known/oauth-protected-resource/mcp`` and to stamp
    ``WWW-Authenticate`` on 401s; T5 verifies the rendered document.
    """
    mcp = MCPServer(
        name=name,
        debug=(log_level == "DEBUG"),
        log_level=log_level,  # type: ignore[arg-type]
        instructions=(
            "Read, write, delete, append to, patch, move, list, "
            "check existence of, diff, enumerate, search, and "
            "flip checkbox tasks on SilverBullet pages. Fourteen "
            "always-on tools (`read_page`, `page_exists`, "
            "`write_page`, `create_page`, `delete_page`, "
            "`append_to_page`, `prepend_to_page`, "
            "`patch_page_lines`, `patch_page_replace`, "
            "`move_page`, `list_pages`, `diff_pages`, "
            "`check_task`, `list_tasks`) plus up to six "
            "journal-gated tools (`journal_histogram`, "
            "`tag_summary`, `recent_pages`, "
            "`pages_touching_topic`, `search_pages`, "
            "`find_backlinks`) when the operator enables "
            "the journal surface, plus one resource "
            "template `silverbullet://page/{name}` "
            "for attaching page bodies to conversation context. "
            "The four read-modify-write tools "
            "(`append_to_page`, `patch_page_lines`, "
            "`patch_page_replace`, `check_task`) accept "
            "`dry_run=True` (T26) to preview the patch without "
            "committing. `list_pages` returns the full meta "
            "envelope per row (`{name, etag, size_bytes, "
            "last_modified_ms, created_ms}`); set "
            "`MCP_SILVERBULLET_LIST_PAGES_HYDRATE_ETAGS=1` to "
            "hydrate the etag from a per-page GET (T28 "
            "opt-in). `diff_pages` (T27) takes one page plus "
            "either `other_name` (a second page to diff "
            "against) or `other_body` (a literal string) and "
            "returns a line-based unified diff alongside the "
            "read-side envelopes for both pages. `list_tasks` "
            "(T29) enumerates checkbox bullets on a page "
            "(`list_tasks(page=\"name\")`) or across the "
            "whole space (`list_tasks(prefix=\"Daily\")`, "
            "requires the journal surface); the per-page "
            "form is always available via `GET /.fs/{page}`, "
            "the space-walk form requires "
            "`MCP_SILVERBULLET_JOURNAL_TOOLS=1` plus "
            "`MCP_SILVERBULLET_SPACE_PATH`. `check_task` "
            "(T30) flips a checkbox bullet's state by its "
            "wikilink ref (`check_task(page, ref, "
            "state=\"done\")`); the same read-modify-write "
            "contract as the patch tools, with `state` in "
            "{\"done\", \"todo\", \"cancelled\"}. Page "
            "names passed to any `name`-taking tool "
            "(`read_page` / `page_exists` / `write_page` / "
            "`create_page` / `delete_page` / "
            "`append_to_page` / `prepend_to_page` / "
            "`patch_page_lines` / `patch_page_replace` / "
            "`move_page` / `diff_pages` / `check_task` / "
            "`list_tasks`) without a file extension are "
            "automatically suffixed with `.md` (T39); "
            "names with an existing extension (`Foo.txt`) "
            "pass through unchanged. The bridge surfaces a "
            "`name_resolution` field on success envelopes "
            "when the canonical form differs from the "
            "caller's input, so an agent learns the "
            "convention for its next call."
        ),
        token_verifier=_resolve_verifier(verifier, token),
        auth=AuthSettings(
            issuer_url=resource_url,  # type: ignore[arg-type]
            resource_server_url=resource_url,  # type: ignore[arg-type]
        ),
    )

    register_tools(
        mcp,
        sb_client,
        hydrate_etags=list_pages_hydrate_etags,
        journal_root=(
            Path(journal.space_path)
            if journal is not None and journal.enabled
            else None
        ),
    )
    if journal is not None:
        register_journal_tools(mcp, journal)
    return mcp


def register_tools(
    mcp: MCPServer,
    sb_client: SBClient,
    *,
    hydrate_etags: bool = False,
    journal_root: Path | None = None,
) -> None:
    """Attach the thirteen ``/.fs``-backed tools, the bullet primitive (``list_tasks``), and one resource template to ``mcp``.

    Pulled out of :func:`build_mcp` so tests can build a server and
    call the registration in isolation. ``mcp.tool()`` / ``mcp.resource()``
    are decorators that take the function; eleven of the fourteen tool
    handlers wrap their ``sb_client`` call in
    :func:`_translate_sb_errors`, which maps SB exceptions to
    :exc:`ToolError` per the design doc's status-code mapping.
    ``move_page`` (T22) is one exception: the post-write-delete
    sequence surfaces a partial-failure ``ToolError`` directly
    from the handler so the caller can see "moved body to {new}
    but failed to delete {old}; both now exist" rather than the
    unified 412 wording — the source and destination are distinct
    pages and the caller needs to know which side refused.
    ``page_exists`` (T25) is the other exception: it doesn't go
    through ``_translate_sb_errors`` because 404 is the *answer*
    (not an error) for the existence question, so the handler
    catches the non-404 SB exceptions inline and surfaces them
    with the same wording as the read tool. ``diff_pages`` (T27)
    is the compound case: it has *two* ``_translate_sb_errors``
    blocks (one per read), each keyed on whichever page that
    read targeted (``name`` for the first, ``other_name`` for
    the second), so a 404 on either side surfaces as
    ``ToolError("page not found: <that page's name>")``. ``list_tasks``
    (T29) is the always-on per-page form: the space-walk
    variant requires ``journal_root`` and falls back to a
    ``ToolError`` when ``journal_root`` is ``None`` and the
    caller didn't name a page. The
    resource template uses the SDK's separate ``ResourceError``
    shapes (JSON-RPC protocol errors vs tool-handler
    ``is_error=True``) and keeps its own translation.

    ``hydrate_etags`` is the v1.2 T28 opt-in: when ``True``, the
    ``list_pages`` tool issues one GET per row (N+1) to hydrate
    the etag field that SB's list payload omits. Default off;
    threaded from :func:`build_mcp` which reads
    ``MCP_SILVERBULLET_LIST_PAGES_HYDRATE_ETAGS``.

    ``journal_root`` is the v1.2 T29 space-walk opt-in: when set,
    the ``list_tasks`` tool's "no page given" branch walks the
    local SB space directory (same shape as
    :func:`mcp_silverbullet.journal._list_tasks_for_space`).
    ``None`` (default, or when the journal gate is off) makes
    the space-walk branch surface a ``ToolError``; the per-page
    form is always available because it routes through
    ``sb_client.read_page`` and doesn't need direct FS access.

    The journal surface (T11/T12) is gated separately — see
    :func:`mcp_silverbullet.journal.register_journal_tools`, called by
    :func:`build_mcp` only when the journal config says the gate is on.
    """

    @mcp.tool(
        title="Read page",
        description=(
            "Read the markdown body and metadata of a SilverBullet "
            "page. Returns `{body, etag, size_bytes, "
            "last_modified_ms}` so the caller can chain edits "
            "without a follow-up round-trip to learn the page's "
            "ETag or current size (T24 — read-side widening of the "
            "T23 acknowledgement envelope, drops `name` since the "
            "caller passed it in and `created_ms` since reads have "
            "no create-vs-update distinction to surface). "
            "`size_bytes` and `last_modified_ms` are `None` when "
            "SB stripped the `X-Content-Length` / `X-Last-Modified` "
            "response headers (older SB / proxy). Pages containing "
            "Space Lua template syntax (e.g. `${template.each(...)}`) "
            "are returned as raw markdown source, never as rendered "
            "output — the bridge is a transport, not a renderer "
            "(T41). Returns 404-equivalent ToolError if the page "
            "is missing."
        ),
    )
    async def read_page(name: str) -> dict[str, object]:
        # T39: normalize the name (strip whitespace, append ``.md``
        # to bare names) before the SB round trip so an agent that
        # passes ``"Foo"`` resolves to ``Foo.md`` and gets the body
        # it expected. The ``name_resolution`` payload surfaces
        # back to the agent so it can learn the convention for its
        # next call.
        resolved_name = _normalize_page_name(name)
        async with _translate_sb_errors(resolved_name):
            page = await sb_client.read_page(resolved_name)
        payload = _read_meta_to_payload(page)
        payload.update(_name_resolution_payload(name, resolved_name))
        return payload

    @mcp.tool(
        title="Page exists",
        description=(
            "Cheap existence check for a SilverBullet page. "
            "Returns `true` if the page exists, `false` if it "
            "doesn't. Issues `GET /.fs/{name}` and discards the "
            "body, so it's cheaper than `read_page` for the "
            "\"does this page exist?\" question (no body bytes "
            "are materialized). A `false` answer is definitive "
            "— 404 means the page isn't there. A 5xx surfaces as "
            "`ToolError(\"silverbullet error: <status>\")` so the "
            "caller can distinguish \"no, proceed with create\" "
            "from \"SB is broken, don't make decisions\". If the "
            "caller also needs the etag / size / body, "
            "`read_page` is one round trip away; this tool is "
            "for the existence-only case. ToolError wording on "
            "timeouts matches the rest of the bridge: "
            "\"silverbullet request timed out\"."
        ),
    )
    async def page_exists(name: str) -> bool:
        # T39: normalize the name (strip whitespace, append
        # ``.md`` to bare names) before the SB round trip. The
        # tool's return type is ``bool`` so there is no envelope
        # to attach a ``name_resolution`` field to; the agent
        # learns the convention by reading the description (the
        # suffix-convention note lives in T41's
        # ``MCPServer.instructions`` addition).
        resolved_name = _normalize_page_name(name)
        # ``exists_page`` swallows ``PageNotFound`` internally and
        # returns ``False``; we don't go through
        # :func:`_translate_sb_errors` because that helper turns
        # 404 into a ``ToolError`` (the right behaviour for the
        # read/write tools, wrong for the existence question).
        # Other SB exceptions (412 / 413 / 5xx) and httpx timeouts
        # still need translating, so we wrap them inline — same
        # wording as :func:`_translate_sb_errors`, but without the
        # 404 clause. (``PreconditionFailed`` and ``BodyTooLarge``
        # are highly unusual on a GET, but if SB / a proxy ever
        # behaves oddly we still want a sensible ``ToolError``
        # rather than an unhandled exception.)
        try:
            return await sb_client.exists_page(resolved_name)
        except PreconditionFailed as exc:
            raise ToolError(
                "precondition failed; check if_match/if_none_match"
            ) from exc
        except BodyTooLarge as exc:
            raise ToolError(
                f"body too large: limit is {_BODY_LIMIT_MIB} MiB"
            ) from exc
        except ServerError as exc:
            raise ToolError(str(exc)) from exc
        except httpx.TimeoutException as exc:
            raise ToolError("silverbullet request timed out") from exc

    @mcp.tool(
        title="Write page",
        description=(
            "Create or update a SilverBullet page. `if_match=\"*\"` "
            "requires the page to exist; `if_match=<etag>` requires "
            "the body hash to match. Returns the write "
            "acknowledgement `{name, etag, size_bytes, "
            "last_modified_ms, created_ms}` so the caller can chain "
            "edits without a follow-up read (T23). 412-equivalent "
            "ToolError on precondition failure, 413 on body > 4 MiB. "
            "Empty / whitespace-only `name` upfront "
            "`ToolError(\"name must not be empty\")`; empty / "
            "whitespace-only `content` upfront "
            "`ToolError(\"content must not be empty\")` (T40)."
        ),
    )
    async def write_page(
        name: str,
        content: str,
        if_match: str | None = None,
    ) -> dict[str, object]:
        # T40: cheap, no-read input validation first. An empty
        # ``name`` is almost certainly a caller bug; surface it
        # loudly upfront before any SB round trip. The guard
        # fires *before* :func:`_normalize_page_name` (next) so a
        # caller passing ``name=""`` still sees
        # ``ToolError("name must not be empty")`` rather than the
        # normalized form ``".md"`` silently succeeding.
        _validate_nonempty_name(name)
        # T40: same guard for ``content`` — a zero-byte write is
        # almost certainly a caller bug (``write_page`` is a
        # overwrite-or-create tool, not a "set empty" tool; for
        # an empty body the caller wants ``delete_page`` instead).
        # The wording matches :func:`append_to_page` /
        # :func:`prepend_to_page`'s existing ``content must not
        # be empty`` style.
        _validate_nonempty_value(content, label="content")
        # T39: normalize the name (strip whitespace, append
        # ``.md`` to bare names) before the SB round trip. The
        # ``name_resolution`` field on the response envelope
        # tells the agent what the bridge changed so it can
        # learn the convention for its next call.
        resolved_name = _normalize_page_name(name)
        # T36: cap the body size before the SB round trip so an
        # oversized write surfaces a clear ``body too large``
        # ``ToolError`` with the remediation hint rather than a
        # deferred failure at SB.
        _check_body_size(content)
        async with _translate_sb_errors(resolved_name):
            meta = await sb_client.write_page(
                resolved_name, content, if_match=if_match
            )
        # T31b: post-write concurrency-token verification. Runs
        # only on 200 writes where ``if_match`` was a concrete
        # etag (``"*"`` and ``None`` are no-ops per the helper's
        # contract); 412 still wins on SBs that honor
        # ``If-Match`` via :func:`_translate_sb_errors`. The
        # helper degrades gracefully on transient re-read
        # failures so a flaky SB doesn't surface false-positive
        # concurrency errors.
        await _verify_concurrency_token(
            sb_client,
            resolved_name,
            post_write_meta=meta,
            expected_etag=if_match,
        )
        payload = _write_meta_to_payload(meta)
        payload.update(_name_resolution_payload(name, resolved_name))
        return payload

    @mcp.tool(
        title="Create page",
        description=(
            "Create a SilverBullet page (refuse to overwrite). "
            "Distinct from `write_page`'s overwrite-or-create "
            "default: `create_page` is the right tool when the "
            "agent has a specific intent to *create* a page "
            "and a collision means a programming bug, not a "
            "silent overwrite. Returns the write acknowledgement "
            "`{name, etag, size_bytes, last_modified_ms, "
            "created_ms}` (T23) on success — same envelope as "
            "`write_page`, so an agent that learns the shape "
            "once has it for both tools. Errors: empty / "
            "whitespace-only `name` upfront "
            "`ToolError(\"name must not be empty\")`; page "
            "already exists → `ToolError(\"page already exists: "
            "{name}; use write_page to overwrite\")` (a clear "
            "next-tool hint rather than the generic 412 wording "
            "the agent would have to pattern-match on). "
            "Implemented as `write_page(name, content, "
            "if_match=\"*\")` + 412 → `already_exists` "
            "translation; the underlying ``If-Match: *`` is the "
            "same primitive `write_page` accepts as a caller-"
            "facing argument, just specialized at the tool "
            "boundary. `if_match` is implied (``\"*\"``); an "
            "explicit precondition would be a misuse of the "
            "create semantic and is not exposed — agents that "
            "want to write-with-precondition call "
            "`write_page` directly."
        ),
    )
    async def create_page(
        name: str,
        content: str,
    ) -> dict[str, object]:
        # Cheap upfront guard: an empty name is almost
        # certainly a caller bug (the caller forgot to fill
        # in the page name); surface it loudly before the
        # round trip. T40 lifts this guard into the shared
        # :func:`_validate_nonempty_name` helper so the
        # wording matches the other tools. The helper fires
        # *before* T39's name normalization (next), so a
        # caller passing ``name=""`` still sees the loud
        # ``ToolError("name must not be empty")`` rather
        # than the normalized form ``".md"`` silently
        # succeeding.
        _validate_nonempty_name(name)
        # T39: normalize the name (strip whitespace, append
        # ``.md`` to bare names) before the SB round trip.
        # The ``name_resolution`` field on the response
        # envelope tells the agent what the bridge changed
        # so it can learn the convention for its next call.
        resolved_name = _normalize_page_name(name)
        # T36: cap the body size before the SB round trip so an
        # oversized create surfaces a clear ``body too large``
        # ``ToolError`` with the remediation hint rather than a
        # deferred failure at SB.
        _check_body_size(content)
        # ``create_page`` always uses ``if_match="*"`` to
        # require existence (refuse to overwrite). The
        # ``if_match="*"`` path opts out of T31b's post-
        # write verification helper (no etag to compare
        # against), which means on SBs that don't honor
        # ``If-Match`` (T31's negative finding on this dev
        # box) the silent-overwrite case is not caught by
        # the helper. The map's T32 charter is the clean
        # one: the ``If-Match: *`` primitive is supposed
        # to enforce existence; on SBs that don't honor
        # it, the primitive is broken and ``create_page``
        # silently overwrites. A T32a follow-up could
        # close the gap with an ``exists_page`` round trip
        # before the PUT (extra cost on the happy path for
        # a rare edge case), but the T32 ticket's charter
        # is the 412 → ``already_exists`` translation
        # only. The honest wire shape is one that maps
        # cleanly to SBs that *do* honor ``If-Match``;
        # the silent-overwrite case is a documented
        # limitation, not a hidden bug.
        async with _translate_sb_errors(resolved_name):
            try:
                meta = await sb_client.write_page(
                    resolved_name, content, if_match="*"
                )
            except PreconditionFailed as exc:
                # SB honored ``If-Match`` — page definitely
                # already exists. Translate to the
                # ``already_exists`` wording with the
                # next-tool hint. ``from exc`` preserves
                # the original ``PreconditionFailed`` in
                # the traceback for debugging. The other
                # SB error types (404 / 5xx / timeout /
                # ``BodyTooLarge``) are caught by
                # ``_translate_sb_errors`` for the standard
                # ``page not found: {name}`` /
                # ``server error`` wording — the
                # ``PreconditionFailed`` translation is
                # specific to ``create_page``'s
                # refuse-to-overwrite contract.
                raise ToolError(
                    f"page already exists: {resolved_name}; "
                    f"use write_page to overwrite"
                ) from exc
        payload = _write_meta_to_payload(meta)
        payload.update(_name_resolution_payload(name, resolved_name))
        return payload

    @mcp.tool(
        title="Delete page",
        description=(
            "Delete a SilverBullet page (hard delete; SB has no "
            "trash layer). `if_match=\"*\"` requires the page to "
            "exist; `if_match=<etag>` requires the body hash to "
            "match. Returns the write acknowledgement `{name, "
            "etag, size_bytes=None, last_modified_ms=None, "
            "created_ms=None}` (T23) — DELETE doesn't echo the "
            "body length or timestamps per the design doc, so "
            "those fields are `None`. The ETag (when present) "
            "echoes the deleted body's hash so the caller can "
            "confirm what was removed. Empty / whitespace-only "
            "`name` upfront `ToolError(\"name must not be empty\")` "
            "(T40); 404-equivalent ToolError if the page is "
            "missing."
        ),
    )
    async def delete_page(
        name: str,
        if_match: str | None = None,
    ) -> dict[str, object]:
        # T40: cheap, no-read input validation first. An empty
        # ``name`` is almost certainly a caller bug; surface it
        # loudly upfront before any SB round trip. The guard
        # fires *before* :func:`_normalize_page_name` (next) so a
        # caller passing ``name=""`` still sees
        # ``ToolError("name must not be empty")`` rather than the
        # normalized form ``".md"`` silently succeeding.
        _validate_nonempty_name(name)
        # T39: normalize the name (strip whitespace, append
        # ``.md`` to bare names) before the SB round trip. The
        # ``name_resolution`` field on the response envelope
        # tells the agent what the bridge changed so it can
        # learn the convention for its next call.
        resolved_name = _normalize_page_name(name)
        async with _translate_sb_errors(resolved_name):
            meta = await sb_client.delete_page(
                resolved_name, if_match=if_match
            )
        # T31b: post-delete verification. Per the T31b ticket
        # docstring, ``delete_page`` gets a *lighter*
        # verification: the helper re-reads the source, hits 404
        # (we just deleted it), and short-circuits to no-op via
        # its ``except PageNotFound: return`` branch. The call
        # is here for symmetry with the other tools and to
        # document the T31b contract — there's no operational
        # concurrency check possible after a delete.
        await _verify_concurrency_token(
            sb_client,
            resolved_name,
            post_write_meta=meta,
            expected_etag=if_match,
        )
        payload = _write_meta_to_payload(meta)
        payload.update(_name_resolution_payload(name, resolved_name))
        return payload

    @mcp.tool(
        title="Append to page",
        description=(
            "Append text to the end of a SilverBullet page. The tool "
            "inserts a single newline between the existing body and "
            "the new text when the body doesn't already end in one; "
            "if it does, the new text is concatenated verbatim. The "
            "tool adds exactly one separator in either case — a "
            "caller-supplied leading newline is preserved unchanged, "
            "so `append_to_page(name, \"\\nworld\")` against a body "
            "of `\"hello\"` produces `\"hello\\n\\nworld\"` (one "
            "separator from the tool, one from the caller). Returns "
            "the write acknowledgement `{name, etag, size_bytes, "
            "last_modified_ms, created_ms}` so the caller can chain "
            "edits without a follow-up read (T23). `if_match=\"*\"` "
            "requires the page to exist; `if_match=<etag>` requires "
            "the body hash to match (protects against concurrent "
            "appends landing out of order). 404-equivalent ToolError "
            "if the page is missing; 412 if the precondition fails; "
            "413 if the combined body exceeds 4 MiB. "
            "`dry_run=True` (T26) returns `{dry_run: True, "
            "original: str, patched: str, diff: str}` without "
            "writing — the read still happens, the in-memory patch "
            "is computed, `if_match=<etag>` is checked against the "
            "read's etag (a stale etag raises 412-equivalent "
            "ToolError so the caller doesn't think a doomed write "
            "would have succeeded), and the tool reports back what "
            "would have changed. `dry_run=True` with `if_match=\"*\"` "
            "is the same as a live `if_match=\"*\"`: a missing page "
            "404s on the read. Empty / whitespace-only `text` "
            "upfront `ToolError(\"text must not be empty\")` (T40). "
            "**T47 auto-retry**: by default the tool retries up to "
            "`max_retries=3` times when the post-write verification "
            "helper fires `concurrent edit detected` (a real "
            "concurrent writer touched the page between the "
            "bridge's PUT and the verification re-read). On each "
            "retry the tool re-reads the body, re-derives the "
            "appended body from the *current* page state, and "
            "re-PUTs. Pass `max_retries=0` to opt out and see the "
            "raw 412. `find not found in body` and `page not found` "
            "errors surface unchanged (the bridge doesn't retry on "
            "anchor-mismatch or 404)."
        ),
    )
    async def append_to_page(
        name: str,
        text: str,
        if_match: str | None = None,
        dry_run: bool = False,
        max_retries: int = 3,
    ) -> dict[str, object]:
        # An empty append is almost certainly a caller bug (the
        # caller meant to write something and forgot to fill it in);
        # surface it loudly upfront so the read-modify-write round
        # trip isn't wasted on a no-op. ``write_page(name, content)``
        # is the right tool for "create with this body" and
        # ``append_to_page(name, "")`` would only ever mean that.
        # T40: shared helper threaded here so the wording matches
        # the other tools (``content must not be empty``,
        # ``new_string must not be empty``, etc.).
        _validate_nonempty_value(text, label="text")
        # T39: normalize the name (strip whitespace, append
        # ``.md`` to bare names) before the SB round trip. The
        # ``name_resolution`` field on the response envelope
        # (live or dry-run) tells the agent what the bridge
        # changed so it can learn the convention for its next
        # call.
        resolved_name = _normalize_page_name(name)
        # T36: cap the body size before the SB round trip.
        _check_body_size(text)
        # T47: thread the entire read-modify-write block through
        # the auto-retry helper. The closure re-reads on each
        # iteration so the appended text lands against the
        # page's *current* state.
        async def attempt() -> dict[str, object]:
            async with _translate_sb_errors(resolved_name):
                page = await sb_client.read_page(resolved_name)
                body = page.body or ""
                new_body = (
                    body + "\n" + text
                    if body and not body.endswith("\n")
                    else body + text
                )
                if dry_run:
                    # T26: validate ``if_match`` against the
                    # read's etag *here* because no PUT happens
                    # to do it on the server.
                    # ``if_match="*"`` means "require existence"
                    # — the read 404s on a missing page, so
                    # the helper no-ops. ``if_match=None`` is
                    # unconditional. A concrete-etag mismatch
                    # raises the same 412 wording the live
                    # path would surface when SB returned 412,
                    # so the agent sees one shape across both
                    # paths.
                    _validate_if_match_on_read(page.etag, if_match)
                    payload = _dry_run_payload(body, new_body)
                    payload.update(
                        _name_resolution_payload(name, resolved_name)
                    )
                    return payload
                # T31b: thread the read's etag into
                # ``if_match`` when the caller passed ``None``,
                # so a concurrent edit between read and write
                # fails 412 on SBs that honor ``If-Match`` (or
                # the post-write verification below on SBs
                # that don't). The caller can still pass an
                # explicit ``if_match`` (real or synthesized)
                # and bypass the auto-thread — the post-write
                # verification uses that explicit value
                # verbatim.
                write_if_match = (
                    if_match if if_match is not None else page.etag
                )
                meta = await sb_client.write_page(
                    resolved_name, new_body, if_match=write_if_match
                )
            await _verify_concurrency_token(
                sb_client,
                resolved_name,
                post_write_meta=meta,
                expected_etag=write_if_match,
                dry_run=dry_run,
            )
            payload = _write_meta_to_payload(meta)
            payload.update(_name_resolution_payload(name, resolved_name))
            return payload

        return await _auto_retry_on_concurrent_edit(
            attempt, max_retries=max_retries
        )

    @mcp.tool(
        title="Prepend to page",
        description=(
            "Prepend text to the top of a SilverBullet page "
            "with YAML frontmatter awareness. Two positions:\n\n"
            "* `position=\"after_frontmatter\"` (default) — "
            "insert the new content *between* the closing "
            "`---` of the frontmatter block and the first body "
            "line. This is the human-meaningful default for "
            "journal / daily-notes pages with YAML frontmatter: "
            "the new content lands at the top of the page "
            "body, *above* the visible content but *below* "
            "the frontmatter (frontmatter stays at the very "
            "top, where frontmatter consumers expect to find "
            "it). For a page without frontmatter, this is "
            "equivalent to `position=\"top\"` — the new "
            "content lands at the absolute top.\n\n"
            "* `position=\"top\"` — insert the new content "
            "*above* the frontmatter, at the absolute top of "
            "the file. Use this when the caller explicitly "
            "wants to push frontmatter down (rare; most "
            "frontmatter is configuration that consumers "
            "expect at the top, so this is almost always a "
            "bug). For a page without frontmatter, this is "
            "equivalent to `position=\"after_frontmatter\"`.\n\n"
            "Mirrors `append_to_page`'s read-modify-write + "
            "`dry_run` shape: the read happens, the in-memory "
            "splice is computed (frontmatter-aware per the "
            "rules above), `if_match=<etag>` is checked "
            "against the read's etag (a stale etag raises "
            "412-equivalent `ToolError`), and the tool "
            "either writes the new body (`dry_run=False`) or "
            "returns the `{dry_run, original, patched, diff}` "
            "preview (`dry_run=True`). Returns the T23 ack "
            "envelope (`{name, etag, size_bytes, "
            "last_modified_ms, created_ms}`) on success.\n\n"
            "Errors: empty / whitespace-only `content` "
            "upfront `ToolError(\"content must not be empty\")`; "
            "unknown `position` upfront "
            "`ToolError(\"position must be one of: "
            "after_frontmatter, top\")`; 412 on stale "
            "`if_match`; 404 on missing page (standard "
            "wording); 413 on a body > 4 MiB (standard "
            "wording). Frontmatter detection: a leading "
            "`---\\n…\\n---\\n` block (LF or CRLF). A page "
            "that opens with `---` but doesn't close it (a "
            "malformed frontmatter block) is treated as "
            "no-frontmatter — the new content lands at the "
            "absolute top, same as a page with no frontmatter "
            "at all. The same \"raw text, no parser\" pattern "
            "the rest of the bridge uses; no YAML library is "
            "pulled in."
        ),
    )
    async def prepend_to_page(
        name: str,
        content: str,
        position: str = "after_frontmatter",
        if_match: str | None = None,
        dry_run: bool = False,
        max_retries: int = 3,
    ) -> dict[str, object]:
        # Cheap, no-read input validation first. An empty
        # content is almost certainly a caller bug (the
        # caller meant to prepend something and forgot to
        # fill it in); surface it loudly upfront so the
        # read-modify-write round trip isn't wasted. Mirrors
        # the empty-``text`` guard on ``append_to_page``.
        # T40: shared helper threaded here for wording consistency
        # across tools (``text must not be empty``,
        # ``find must not be empty``, ``ref must not be empty``).
        _validate_nonempty_value(content, label="content")
        # Validate ``position`` upfront — an unknown value
        # is almost certainly a typo (``"topmost"``,
        # ``"first"``, ``"above_frontmatter"``, ...). The
        # two-mode shape mirrors ``append_to_page``'s
        # ``dry_run`` knob: small extra surface, big
        # usability win.
        if position not in ("after_frontmatter", "top"):
            raise ToolError(
                "position must be one of: "
                "after_frontmatter, top"
            )
        # T39: normalize the name (strip whitespace, append
        # ``.md`` to bare names) before the SB round trip.
        # The ``name_resolution`` field on the response
        # envelope (live or dry-run) tells the agent what
        # the bridge changed so it can learn the convention
        # for its next call.
        resolved_name = _normalize_page_name(name)
        # T36: cap the body size before the SB round trip.
        _check_body_size(content)

        # T47: thread the entire read-modify-write block
        # through the auto-retry helper. The closure
        # re-reads on each iteration so the prepended text
        # lands against the page's *current* state (the
        # frontmatter-aware splice recomputes against the
        # latest body, including any concurrent edits).
        async def attempt() -> dict[str, object]:
            async with _translate_sb_errors(resolved_name):
                page = await sb_client.read_page(resolved_name)
                body = page.body or ""
                # Compute the splice per ``position``. The
                # ``_split_frontmatter_block`` helper returns
                # ``(frontmatter_or_None, rest)`` where
                # ``None`` is the canonical "no frontmatter"
                # signal. The ``position`` knob affects only
                # the case where frontmatter is present;
                # without frontmatter, both
                # ``after_frontmatter`` and ``top`` produce
                # the same splice (new content at absolute
                # top).
                frontmatter, rest = _split_frontmatter_block(body)
                if position == "top" or frontmatter is None:
                    # Either the caller explicitly wanted
                    # absolute-top, or there's no frontmatter
                    # to anchor against. ``new_content + body``
                    # in both cases.
                    new_body = content + body
                else:
                    # ``after_frontmatter`` with frontmatter
                    # present: ``frontmatter + content + rest``.
                    new_body = frontmatter + content + rest
                if dry_run:
                    # T26: validate ``if_match`` against the
                    # read's etag *here* because no PUT happens.
                    # Same shape as the other read-modify-write
                    # tools' dry-run paths.
                    _validate_if_match_on_read(page.etag, if_match)
                    payload = _dry_run_payload(body, new_body)
                    payload.update(
                        _name_resolution_payload(name, resolved_name)
                    )
                    return payload
                # T31b: thread the read's etag into ``if_match``
                # when the caller passed ``None``, so a
                # concurrent edit between read and write fails
                # 412 on SBs that honor ``If-Match`` (or the
                # post-write verification below on SBs that
                # don't). Same auto-thread pattern as
                # ``append_to_page`` / ``patch_page_lines`` /
                # ``patch_page_replace``.
                write_if_match = (
                    if_match if if_match is not None else page.etag
                )
                meta = await sb_client.write_page(
                    resolved_name, new_body, if_match=write_if_match
                )
            await _verify_concurrency_token(
                sb_client,
                resolved_name,
                post_write_meta=meta,
                expected_etag=write_if_match,
                dry_run=dry_run,
            )
            payload = _write_meta_to_payload(meta)
            payload.update(_name_resolution_payload(name, resolved_name))
            return payload

        return await _auto_retry_on_concurrent_edit(
            attempt, max_retries=max_retries
        )

    @mcp.tool(
        title="Patch page (lines)",
        description=(
            "Replace lines `start_line..end_line` (1-indexed, "
            "inclusive) of a SilverBullet page with `new_content`. "
            "Line splitting: the body is split on `\\n` (single "
            "newline, matching how SB stores text — no universal-"
            "newline handling) and a trailing empty element is "
            "dropped, so `\"a\\nb\\n\"` is two lines (`a`, `b`), "
            "not three. Pass `new_content=\"\"` to delete the range "
            "without adding a replacement. The page's trailing "
            "newline is preserved iff the body had one and the "
            "patched result is non-empty (editor-style: deleting "
            "lines doesn't strip the file's final `\\n`). "
            "`if_match=\"*\"` requires the page to exist; "
            "`if_match=<etag>` requires the body hash to match "
            "(the same read-modify-write contract as "
            "`append_to_page`). Returns the write acknowledgement "
            "`{name, etag, size_bytes, last_modified_ms, "
            "created_ms}` (T23). `start_line < 1`, `end_line < "
            "start_line`, and `end_line` past the last line all "
            "raise `ToolError` with the page's line count; 404 if "
            "the page is missing; 412 if the precondition fails; "
            "413 if the patched body exceeds 4 MiB. "
            "`dry_run=True` (T26) returns `{dry_run: True, "
            "original: str, patched: str, diff: str}` without "
            "writing — the read still happens, the in-memory patch "
            "is computed (including the trailing-newline "
            "preservation rule above), `if_match=<etag>` is checked "
            "against the read's etag (a stale etag raises "
            "412-equivalent ToolError so the caller doesn't think a "
            "doomed write would have succeeded), and the tool "
            "reports back what would have changed. The pre-read "
            "input-validation errors above still fire on dry-run — "
            "a caller that passes an inverted range shouldn't get a "
            "vague \"would have failed\" back, they get the same "
            "specific ToolError the live path would surface. Empty "
            "/ whitespace-only `name` upfront "
            "`ToolError(\"name must not be empty\")` (T40)."
        ),
    )
    async def patch_page_lines(
        name: str,
        start_line: int,
        end_line: int,
        new_content: str,
        if_match: str | None = None,
        dry_run: bool = False,
        max_retries: int = 3,
    ) -> dict[str, object]:
        # T40: cheap, no-read input validation first. An empty
        # ``name`` is almost certainly a caller bug; surface it
        # loudly upfront before any SB round trip. The guard
        # fires *before* :func:`_normalize_page_name` (below) so
        # a caller passing ``name=""`` still sees
        # ``ToolError("name must not be empty")`` rather than the
        # normalized form ``".md"`` silently succeeding.
        _validate_nonempty_name(name)
        # Cheap, no-read input validation first: a non-positive
        # start_line or an inverted range can't be helped by reading
        # the page (line_count is undefined until then), so the
        # out-of-bounds wording in the ticket doesn't apply. Keep
        # these pre-read errors terse and let the post-read error
        # carry the page's line count.
        if not isinstance(start_line, int) or isinstance(start_line, bool):
            raise ToolError(
                f"start_line must be an int, got {type(start_line).__name__}"
            )
        if not isinstance(end_line, int) or isinstance(end_line, bool):
            raise ToolError(
                f"end_line must be an int, got {type(end_line).__name__}"
            )
        if start_line < 1:
            raise ToolError(f"start_line must be >= 1, got {start_line}")
        if end_line < start_line:
            raise ToolError(
                f"end_line ({end_line}) must be >= start_line ({start_line})"
            )
        # T39: normalize the name (strip whitespace, append
        # ``.md`` to bare names) before the SB round trip. The
        # ``name_resolution`` field on the response envelope
        # (live or dry-run) tells the agent what the bridge
        # changed so it can learn the convention for its next
        # call.
        resolved_name = _normalize_page_name(name)
        # T36: cap the body size before the SB round trip.
        _check_body_size(new_content)

        # T47: thread the entire read-modify-write block
        # through the auto-retry helper. The closure
        # re-reads on each iteration so the line-range patch
        # recomputes against the page's *current* state —
        # if a concurrent edit shifted lines between
        # attempts, the next attempt sees the new line
        # numbers (and surfaces ``out of bounds`` if the
        # page has shrunk past the requested range).
        async def attempt() -> dict[str, object]:
            async with _translate_sb_errors(resolved_name):
                page = await sb_client.read_page(resolved_name)
                body = page.body or ""
                lines, had_trailing_newline = _split_body_lines(body)
                line_count = len(lines)
                if end_line > line_count:
                    raise ToolError(
                        f"line range {start_line}..{end_line} "
                        f"out of bounds for page with "
                        f"{line_count} lines"
                    )
                new_body = _apply_line_patch(
                    lines, start_line, end_line, new_content
                )
                # Preserve the page's trailing newline the
                # way an editor would: ``splitlines``/``join``
                # above drops it as a side effect, so re-
                # attach it iff the body had one and the
                # result is non-empty. An empty patched body
                # has no trailing newline either way.
                if had_trailing_newline and new_body:
                    new_body += "\n"
                if dry_run:
                    # T26: same ``if_match``-on-read
                    # validation as ``append_to_page``. The
                    # dry-run envelope surfaces the *post-
                    # shaping* ``new_body`` (with trailing
                    # newline re-attached), so the diff an
                    # agent sees is exactly the body that
                    # would have been written.
                    _validate_if_match_on_read(page.etag, if_match)
                    payload = _dry_run_payload(body, new_body)
                    payload.update(
                        _name_resolution_payload(name, resolved_name)
                    )
                    return payload
                # T31b: same auto-thread pattern as
                # :func:`append_to_page`. The caller's
                # explicit ``if_match`` wins when present;
                # the read's etag threads through
                # automatically when the caller passed
                # ``None``. The post-write verification
                # below compares the same value against the
                # post-write re-read.
                write_if_match = (
                    if_match if if_match is not None else page.etag
                )
                meta = await sb_client.write_page(
                    resolved_name, new_body, if_match=write_if_match
                )
            await _verify_concurrency_token(
                sb_client,
                resolved_name,
                post_write_meta=meta,
                expected_etag=write_if_match,
                dry_run=dry_run,
            )
            payload = _write_meta_to_payload(meta)
            payload.update(_name_resolution_payload(name, resolved_name))
            return payload

        return await _auto_retry_on_concurrent_edit(
            attempt, max_retries=max_retries
        )

    @mcp.tool(
        title="Patch page (replace)",
        description=(
            "Replace literal occurrences of `find` with `new_string` "
            "in a SilverBullet page. `find` is matched as a plain "
            "substring — no regex, no glob, no fuzzy matching (an "
            "agent that wants regex uses `rg` or Python's `re` "
            "client-side, then calls this tool with the literal "
            "result). `replace_all=False` (the default) errors "
            "instead of silently mass-editing when `find` matches "
            "more than once: `ToolError(\"find matched N times; pass "
            "replace_all=True or narrow find\")`. `replace_all=True` "
            "replaces every occurrence. `find` not in body → "
            "`ToolError(\"find not found in body\")` — a typo in the "
            "find string should not look like success. Empty `find` "
            "is rejected upfront (would match between every "
            "character): `ToolError(\"find must not be empty\")`. "
            "`if_match=\"*\"` requires the page to exist; "
            "`if_match=<etag>` requires the body hash to match "
            "(same read-modify-write contract as "
            "`append_to_page` and `patch_page_lines`). Returns the "
            "write acknowledgement `{name, etag, size_bytes, "
            "last_modified_ms, created_ms}` (T23). 404 if the page "
            "is missing; 412 if the precondition fails; 413 if the "
            "patched body exceeds 4 MiB. "
            "`dry_run=True` (T26) returns `{dry_run: True, "
            "original: str, patched: str, diff: str}` without "
            "writing — the read still happens, the in-memory patch "
            "is computed, `if_match=<etag>` is checked against the "
            "read's etag (a stale etag raises 412-equivalent "
            "ToolError so the caller doesn't think a doomed write "
            "would have succeeded), and the tool reports back what "
            "would have changed. The pre-read input-validation "
            "errors above (``find`` not in body, multiple matches "
            "with ``replace_all=False``) still fire on dry-run — a "
            "caller that passes a bad ``find`` shouldn't get a "
            "vague \"would have failed\" back, they get the same "
            "specific ToolError the live path would surface."
        ),
    )
    async def patch_page_replace(
        name: str,
        find: str,
        new_string: str,
        replace_all: bool = False,
        if_match: str | None = None,
        dry_run: bool = False,
        max_retries: int = 3,
    ) -> dict[str, object]:
        # Cheap, no-read input validation first. ``find == ""`` would
        # match between every character (``"abc".replace("", "X")``
        # is ``"XaXbXcX"``) — almost certainly a caller bug, not
        # the edit they wanted. Surface it loudly upfront so the
        # read-modify-write round trip isn't wasted and the bug is
        # pinned at the call site. Same pattern as
        # :func:`append_to_page`'s ``text must not be empty``.
        # T40: shared helper threaded here for wording consistency
        # across tools.
        _validate_nonempty_value(find, label="find")
        # NOTE: ``new_string=""`` is *legitimately* the "delete
        # every match" path (``"abcdefg".replace("cd", "")`` is
        # ``"abefg"``); the ticket originally proposed guarding
        # it, but that's a documented surface (``pass new_string=""
        # to delete the match``), not a caller bug. T40's actual
        # scope is the four tools with no upfront guards at all;
        # this tool already has the ``find`` guard, which is the
        # half that prevents a runaway match-everywhere.
        # T39: normalize the name (strip whitespace, append
        # ``.md`` to bare names) before the SB round trip. The
        # ``name_resolution`` field on the response envelope
        # (live or dry-run) tells the agent what the bridge
        # changed so it can learn the convention for its next
        # call.
        resolved_name = _normalize_page_name(name)
        # T36: cap the body size before the SB round trip. The cap
        # applies to ``new_string`` (the caller's replacement text),
        # not the post-shaping body, matching the T36 charter's
        # "you tried to write 600 KB" framing.
        _check_body_size(new_string)

        # T47: thread the entire read-modify-write block
        # through the auto-retry helper. The closure
        # re-reads on each iteration so the ``find`` text
        # is re-searched against the page's *current* body
        # — if the anchor still appears in the new body
        # (e.g., the concurrent writer added text *elsewhere*
        # on the page), the patch applies on the next
        # attempt. If the body has drifted too far for the
        # anchor to make sense, ``find not found in body``
        # surfaces to the agent as-is (the auto-retry
        # helper doesn't catch this — see the helper's
        # docstring).
        async def attempt() -> dict[str, object]:
            async with _translate_sb_errors(resolved_name):
                page = await sb_client.read_page(resolved_name)
                body = page.body or ""
                occurrences = body.count(find)
                if occurrences == 0:
                    raise ToolError("find not found in body")
                if not replace_all and occurrences > 1:
                    raise ToolError(
                        f"find matched {occurrences} times; "
                        f"pass replace_all=True or narrow find"
                    )
                # ``str.replace`` handles the literal-substring case
                # without escaping: no regex, no fuzzy match, no
                # escape to forget. The ``count`` parameter threads
                # the ``replace_all`` knob (None = replace all, 1 =
                # first only — same shape as Python's ``str.replace``).
                new_body = body.replace(
                    find, new_string, -1 if replace_all else 1
                )
                if dry_run:
                    # T26: ``if_match`` is validated against the
                    # read's etag here because no PUT happens.
                    # ``find not in body`` and the multiple-match-
                    # with-default errors above already raised, so
                    # by this point we know the patch would have
                    # changed something — the dry-run envelope
                    # surfaces the result.
                    _validate_if_match_on_read(page.etag, if_match)
                    payload = _dry_run_payload(body, new_body)
                    payload.update(
                        _name_resolution_payload(name, resolved_name)
                    )
                    return payload
                # T31b: same auto-thread pattern as the other
                # read-modify-write tools — caller-supplied
                # ``if_match`` wins, read's etag threads through
                # when the caller passed ``None``. The post-write
                # verification helper covers SBs that don't honor
                # ``If-Match``.
                write_if_match = (
                    if_match if if_match is not None else page.etag
                )
                meta = await sb_client.write_page(
                    resolved_name, new_body, if_match=write_if_match
                )
            await _verify_concurrency_token(
                sb_client,
                resolved_name,
                post_write_meta=meta,
                expected_etag=write_if_match,
                dry_run=dry_run,
            )
            payload = _write_meta_to_payload(meta)
            payload.update(_name_resolution_payload(name, resolved_name))
            return payload

        return await _auto_retry_on_concurrent_edit(
            attempt, max_retries=max_retries
        )

    @mcp.tool(
        title="Move page",
        description=(
            "Rename a SilverBullet page from `name` to `new_name`, "
            "preserving the body. Implemented as `read_page(name) → "
            "write_page(new_name, body, if_none_match=True) → "
            "delete_page(name, if_match=<etag>)`: write-then-delete "
            "so the partial-failure case (delete fails after write "
            "succeeds) leaves the body at `new_name` rather than "
            "losing it. `if_match` on the outer call guards the "
            "source read-delete pair (the etag from the read is "
            "threaded into `delete_page`'s `If-Match`, so a concurrent "
            "edit between read and delete fails the move with 412 "
            "rather than silently moving the wrong body); the read "
            "carries no precondition. The destination write always "
            "uses `If-None-Match: *` — `move_page` never silently "
            "overwrites an existing page, the caller would just "
            "compose `read_page(new_name) → write_page(new_name, "
            "merged) → delete_page(name)` themselves if they wanted "
            "that. `name == new_name` is a no-op that verifies "
            "existence via a read (no write/delete round trip) and "
            "returns the page's full acknowledgement envelope (T23); "
            "the no-op never raises 412, even when the caller passes "
            "`if_match=<stale_etag>` and the page has drifted — no "
            "write happens so no precondition check fires (T41). "
            "Callers that need to verify the etag on a same-name "
            "no-op should chain "
            "`write_page(name, body, if_match=\"<etag>\")` themselves. Returns the new page's write "
            "acknowledgement `{name, etag, size_bytes, "
            "last_modified_ms, created_ms}` on success (T23), "
            "where `name` is the *destination* name. Errors: "
            "404-equivalent ToolError if the source is missing "
            "(`page not found: {name}`), 412 from the destination "
            "write surfaces as `destination page already exists: "
            "{new_name}; refusing to overwrite` (clearer than the "
            "generic 412 wording because the source and destination "
            "are different pages — the caller needs to know which "
            "side refused), 412 from the source delete after a "
            "successful destination write surfaces as `moved body "
            "to {new_name} but failed to delete {name}: <reason>; "
            "both now exist` so the caller can clean up the "
            "duplicate, 413 if the body exceeds 4 MiB on the "
            "destination write. Empty / whitespace-only `name` or "
            "`new_name` upfront `ToolError(\"name must not be "
            "empty\")` (T40) — both args share the guard so the "
            "wording is consistent."
        ),
    )
    async def move_page(
        name: str,
        new_name: str,
        if_match: str | None = None,
        max_retries: int = 3,
    ) -> dict[str, object]:
        # T40: cheap, no-read input validation first. An empty
        # ``name`` or ``new_name`` is almost certainly a caller
        # bug; surface it loudly upfront before any SB round
        # trip. The guards fire *before*
        # :func:`_normalize_page_name` (below) so a caller
        # passing ``name=""`` or ``new_name=""`` still sees
        # ``ToolError("name must not be empty")`` rather than
        # the normalized form ``".md"`` silently succeeding.
        _validate_nonempty_name(name)
        _validate_nonempty_name(new_name)
        # T39: normalize both the source and destination names
        # (strip whitespace, append ``.md`` to bare names) before
        # the SB round trips. The source's ``name_resolution``
        # field on the response envelope tells the agent what
        # the bridge changed; the destination's normalization is
        # implicit via ``payload["name"]`` (which echoes
        # ``resolved_new_name`` on the success path).
        resolved_name = _normalize_page_name(name)
        resolved_new_name = _normalize_page_name(new_name)
        # Same-name short-circuit: ``name == new_name`` is a no-op
        # that returns the page's current acknowledgement without a
        # write/delete round-trip. The caller is asking us to rename
        # a page to itself — there is nothing to do, and running the
        # dance would risk spurious 412s on the source delete (we'd
        # have just written a fresh body to ``new_name`` — which is
        # also ``name`` — so the etag from the read would be stale).
        # Compare the *resolved* names: ``move_page("Foo", "Foo")``
        # and ``move_page("Foo.md", "Foo")`` are both no-ops once
        # both sides normalize to ``Foo.md``.
        if resolved_name == resolved_new_name:
            async with _translate_sb_errors(resolved_name):
                # Same-name is a no-op, but a missing page would
                # otherwise silently succeed. ``read_page`` is the
                # cheapest existence check (no etag round-trip;
                # ``list_pages`` doesn't carry etags on the v1 sync
                # payload). T23: read_page now returns the page's
                # full meta, so the same-name no-op can hand the
                # caller a real acknowledgement — size, timestamps,
                # etag — without an extra round trip.
                #
                # ``if_match`` is intentionally not honored here:
                # the precondition guards the source delete, which
                # doesn't run on a same-name no-op, and ``read_page``
                # doesn't accept a precondition. Callers that want
                # to verify the etag should chain
                # ``write_page(name, body, if_match=<etag>)``
                # themselves.
                page = await sb_client.read_page(resolved_name)
                payload = _write_meta_to_payload(page)
                payload.update(
                    _name_resolution_payload(name, resolved_name)
                )
                return payload

        # T47: thread the entire read-write-delete dance
        # through the auto-retry helper. The closure
        # re-reads the source on each iteration, so a
        # concurrent writer that mutates ``name`` between
        # attempts is picked up by the next read. The
        # ``if_match`` argument threads into the source
        # delete (step 3) verbatim — the auto-retry
        # doesn't re-derive that value, because the caller's
        # intent is the right invariant (a stale ``if_match``
        # should fail, not auto-retry; see the helper's
        # docstring on the standard-412 surface).
        async def attempt() -> dict[str, object]:
            async with _translate_sb_errors(resolved_name):
                # 1. Read the source body. No precondition — the source's
                # ``If-Match`` guard lives on the delete (step 3) and is
                # supplied by the *caller's* outer ``if_match`` argument,
                # not by the etag from this read (which ``read_page``
                # doesn't surface). A caller that wants the move to fail
                # 412 on a concurrent edit must thread the etag in:
                # ``read_page → move_page(name, new_name, if_match=<etag>)``.
                # A 404 here surfaces the standard
                # ``page not found: {name}`` wording.
                page = await sb_client.read_page(resolved_name)
                body = page.body or ""
                # T36: cap the about-to-be-written body (the source's
                # body, which becomes the destination's body on
                # ``move_page``) before the destination PUT. The cap is
                # on the source's stored body, not the request — this
                # catches the "move a 600 KB page" case before the
                # destination write. An oversized source page is
                # unusual (the cap is 256 KiB), but the bridge should
                # still surface a clear ``body too large`` error rather
                # than a deferred failure at the destination PUT.
                _check_body_size(body)
                # 2. Write the body to ``new_name``. ``if_none_match=True``
                # makes SB send ``If-None-Match: *`` and refuse if the
                # destination already exists — ``move_page`` is rename,
                # not merge, so a collision is a clear 412 the caller
                # resolves by naming a different destination or by
                # composing the merge themselves. The etag we read in
                # step 1 doesn't apply here (it's the source's etag, not
                # the destination's; the destination didn't exist until
                # this write).
                try:
                    new_meta = await sb_client.write_page(
                        resolved_new_name, body, if_none_match=True
                    )
                except PreconditionFailed as exc:
                    # Destination already exists — surface a clearer
                    # message than the unified 412 wording. The source
                    # hasn't been touched yet, so this is purely a
                    # caller-side decision (pick a different new_name
                    # or merge manually).
                    raise ToolError(
                        f"destination page already exists: "
                        f"{resolved_new_name}; "
                        f"refusing to overwrite"
                    ) from exc
            # 3. Delete the source. This call sits outside the first
            # ``_translate_sb_errors`` block because step 2 already
            # succeeded: a 412 here means the source's etag went stale
            # between read and delete (someone else wrote ``name`` in
            # the gap). That's the atomicity-caveat case the ticket
            # calls out — the body is now at *both* names — and the
            # caller needs a clearer message than the unified 412
            # wording to recover (``read_page(new_name) → write_page(
            # name, …) → delete_page(new_name)``).
            try:
                await sb_client.delete_page(
                    resolved_name, if_match=if_match
                )
            except PreconditionFailed as exc:
                raise ToolError(
                    f"moved body to {resolved_new_name} but failed to "
                    f"delete {resolved_name}: precondition failed; "
                    f"check if_match/if_none_match; both now exist"
                ) from exc
            except PageNotFound as exc:
                # Edge case: ``name`` was deleted between step 1's read
                # and step 3's delete. The body is at ``new_name``,
                # which is what the caller wanted; ``name`` already
                # gone is a feature, not a bug. Surface a clear message
                # rather than the generic 404 wording.
                raise ToolError(
                    f"moved body to {resolved_new_name} but "
                    f"{resolved_name} was already deleted before the "
                    f"cleanup step"
                ) from exc
            except ServerError as exc:
                raise ToolError(
                    f"moved body to {resolved_new_name} but failed to "
                    f"delete {resolved_name}: {exc}; both now exist"
                ) from exc
            except httpx.TimeoutException as exc:
                raise ToolError(
                    f"moved body to {resolved_new_name} but failed to "
                    f"delete {resolved_name}: silverbullet request "
                    f"timed out; both now exist"
                ) from exc
            # T31b: post-delete concurrency verification. Per the
            # T31b ticket docstring, ``move_page`` gets a *lighter*
            # verification: the helper re-reads the source, hits 404
            # (we just deleted it), and short-circuits to no-op via
            # its ``except PageNotFound: return`` branch. The call is
            # here for symmetry with the other tools and to document
            # the T31b contract — there's no operational concurrency
            # check possible after a successful move (the source
            # gone and the destination exists is the intended state).
            # The pre-delete 412 path above (``"moved body to … but
            # failed to delete"``) remains the primary concurrency
            # signal on SBs that honor ``If-Match``; on SBs that
            # don't, the destination write's 200 with the source
            # already mutated out-of-band is the failure mode this
            # helper *can't* detect (the read happened before the
            # destination write, so a write between read and
            # destination-write would not be caught by a post-delete
            # re-read — the body is at ``new_name``, which is what
            # the caller wanted). The T31b ticket explicitly
            # accepts this gap: ``move_page`` is a structural
            # rename, not a guarded edit, and the per-step 412s
            # are the realistic concurrency story.
            await _verify_concurrency_token(
                sb_client,
                resolved_name,
                post_write_meta=new_meta,
                expected_etag=if_match,
            )
            # Successful move: return the destination's acknowledgement.
            # ``new_meta`` already has ``name=resolved_new_name``
            # (write_page threads the name through), so the payload's
            # ``name`` field is the destination, not the source. The
            # ``name_resolution`` field on the envelope surfaces the
            # source's normalization; the destination's normalization
            # is implicit via the echoed ``name`` field.
            payload = _write_meta_to_payload(new_meta)
            payload.update(_name_resolution_payload(name, resolved_name))
            return payload

        return await _auto_retry_on_concurrent_edit(
            attempt, max_retries=max_retries
        )

    @mcp.tool(
        title="List pages",
        description=(
            "List pages in the SilverBullet space, optionally filtered "
            "by `prefix` (substring match of the path's `startswith`) "
            "and/or `contains` (substring anywhere in the page name). "
            "Both filters compose as AND when both are set; either "
            "alone is a single-criterion narrowing; both empty "
            "returns the full listing (v1 default). v1's `prefix` "
            "filter stays at `startswith` semantics — the substring "
            "narrowing lives at `contains` (T37) so the v1 / v1.1 / "
            "v1.2 / v1.3 wire surface is unchanged for callers that "
            "already use `prefix`. v1 does the filter client-side "
            "(server-side Space Lua search is out of scope per T4 of "
            "the prior map). v1.2 T28 widened the return shape from "
            "`[{name, etag}]` (v1.1 minimal subset) to the same "
            "envelope family the read and write tools use: each row "
            "is `{name, etag, size_bytes, last_modified_ms, "
            "created_ms}`. The list payload carries most of those "
            "fields directly from SB's `GET /.fs` response (per "
            "`server/src/handlers/fs.rs::handle_fs_list`), but does "
            "NOT carry an `etag` field on this SB build — the v1 "
            "map's T10 decision documented this. The bridge's "
            "default behaviour is to surface `etag=None` for every "
            "row, same as v1.1 did. Operators who need an `if_match` "
            "round-trip from a list call can opt in to per-page "
            "hydration via "
            "`MCP_SILVERBULLET_LIST_PAGES_HYDRATE_ETAGS=1`: the "
            "bridge then issues one GET per page (N+1 cost) to "
            "hydrate the etag from the page's `ETag` response "
            "header. Hydration is sequential (no fan-out) and "
            "tolerant: a single page 404'ing (deleted between "
            "list and hydrate), 412'ing (proxy / SB misconfig), "
            "or timing out leaves that row's `etag=None` rather "
            "than failing the whole list call — an agent that "
            "needs the etag for a specific page can always "
            "`read_page` it directly. Note: this filter only ever "
            "matches against page *names*; body-content search "
            "lives behind the journal gate (`MCP_SILVERBULLET_JOURNAL_TOOLS=1` "
            "+ `MCP_SILVERBULLET_SPACE_PATH`; see the README's "
            "Discovery tools (journal-gated) section)."
        ),
    )
    async def list_pages(
        prefix: str = "",
        contains: str = "",
    ) -> list[dict[str, object]]:
        async with _translate_sb_errors(""):
            metas = await sb_client.list_pages()
        # Apply the prefix filter *before* hydration: visiting a
        # row the prefix is about to discard anyway is wasted
        # round trips. The v1 design locks the filter as
        # client-side (server-side Space Lua search is out of
        # scope per T4 of the prior map), so we have to filter in
        # Python either way — the order is the only choice, and
        # filter-then-hydrate is the obvious right one. The
        # ``test_list_pages_hydration_runs_after_prefix_filter``
        # test locks this down so a future refactor that
        # re-orders for any reason surfaces as wasted SB load
        # rather than a silent efficiency regression.
        #
        # T37 widens the filter surface: `prefix` keeps its
        # ``startswith`` semantics (unchanged for v1 / v1.1 /
        # v1.2 / v1.3 callers) and a sibling ``contains``
        # parameter does substring matching against the page
        # name. The two compose as AND — a caller that passes
        # both gets the tighter set, never the wider one.
        # Either filter set to "" is a no-op for that criterion;
        # both empty returns the full listing (v1 default).
        # Both filters run before hydration, so a narrow
        # ``contains`` reduces the per-page round-trip count the
        # same way ``prefix`` does (no wasted GETs for rows the
        # filter is about to discard).
        if prefix:
            metas = [m for m in metas if m.name.startswith(prefix)]
        if contains:
            metas = [m for m in metas if contains in m.name]
        if hydrate_etags:
            metas = await _hydrate_list_etags(sb_client, metas)
        # Project each PageMeta down to the T23 write-shape (which
        # is also the T28 list-row shape — same projection, one
        # helper). ``body`` is dropped because list_pages returns
        # meta only; an agent that wants the body reads the page.
        return [_write_meta_to_payload(m) for m in metas]

    @mcp.tool(
        title="Diff pages",
        description=(
            "Compute a unified diff between two SilverBullet pages "
            "or between a page and a literal string. Pass exactly one "
            "of `other_name` (a page to diff against) or `other_body` "
            "(a literal markdown string to diff against). The tool "
            "fetches the first page (and the second page when "
            "`other_name` is given), runs `difflib.unified_diff`, and "
            "returns `{diff, name, other?}` where `diff` is the "
            "unified diff string (empty string when the two bodies "
            "are identical), `name` is the read-side envelope "
            "(`{name, body, etag, size_bytes, last_modified_ms}`) "
            "for the first page, and `other` is the same envelope for "
            "the second page when `other_name` was given. The "
            "second-page envelope carries `name` explicitly because "
            "the caller passed `other_name`, not the second page's "
            "body — the agent needs the name to know which page "
            "the diff's right side came from. The diff is "
            "line-based by default (line-based is the v1.2 standing "
            "preference for `diff_pages`); token-level / word-level "
            "diffing is a v1.3 refinement. Errors: passing neither "
            "or both of `other_name` / `other_body` → "
            "`ToolError(\"pass exactly one of other_name or "
            "other_body\")`; the page-not-found case on either side "
            "→ standard `ToolError(\"page not found: {name}\")` (the "
            "caller knows which side from the wording's `name` "
            "field); 5xx / 412 / 413 / timeout on either read → the "
            "same wording the read tool surfaces."
        ),
    )
    async def diff_pages(
        name: str,
        other_name: str | None = None,
        other_body: str | None = None,
    ) -> dict[str, object]:
        other_name_given = other_name is not None
        other_body_given = other_body is not None
        if other_name_given == other_body_given:
            # Either neither flag is set or both are; the tool needs
            # exactly one. Raise upfront before the read round
            # trip — a caller that confused the two flags shouldn't
            # pay for a wasted GET to learn about the input shape.
            raise ToolError(
                "pass exactly one of other_name or other_body"
            )
        # T39: normalize both names (strip whitespace, append
        # ``.md`` to bare names) before the SB round trips. Each
        # side's ``name_resolution`` field on the response
        # envelope tells the agent what the bridge changed; the
        # canonical names are also echoed on each per-page
        # envelope's ``name`` field (``first``/``second``).
        resolved_name = _normalize_page_name(name)
        resolved_other_name = (
            _normalize_page_name(other_name)
            if other_name_given else None
        )
        # Read the source page inside ``_translate_sb_errors``
        # so 404 / 412 / 5xx / timeout surface as the design doc's
        # ToolError wording (matching the read tool). The second
        # read (when ``other_name`` is given) sits in its own
        # ``_translate_sb_errors`` block keyed on
        # ``resolved_other_name``, so a 404 there surfaces as
        # ``page not found: {resolved_other_name}`` — the agent
        # can tell which side is missing without inspecting the
        # call. Sequential reads, not concurrent:
        # ``difflib.unified_diff`` needs both bodies in hand, and
        # the cost is the same either way for two round trips —
        # ``asyncio.gather`` would only save wall-clock at the cost
        # of two sockets against loopback SB.
        async with _translate_sb_errors(resolved_name):
            first = await sb_client.read_page(resolved_name)
        if other_name_given:
            assert resolved_other_name is not None
            async with _translate_sb_errors(resolved_other_name):
                second = await sb_client.read_page(resolved_other_name)
            other_body = second.body or ""
        # ``difflib.unified_diff`` input order is (original, patched);
        # we diff ``first`` against ``other`` with ``first`` as the
        # ``fromfile`` so the resulting ``-`` lines are deletions
        # from ``first`` and ``+`` lines are additions from
        # ``other``. The diff is line-based by default (T27 standing
        # preference; token-level is v1.3). The line-splitting and
        # trailing-newline shape reuse the same logic as
        # :func:`_dry_run_payload` — split on ``"\\n"`` directly (no
        # universal-newline handling; SB stores LF only) and add a
        # single ``"\\n"`` per ``difflib`` line via ``lineterm=""``
        # + post-process join so the concatenated diff is
        # well-formed.
        first_body = first.body or ""
        diff = "".join(
            line + "\n"
            for line in difflib.unified_diff(
                first_body.split("\n"),
                other_body.split("\n"),
                fromfile=resolved_name,
                tofile=(
                    resolved_other_name
                    if other_name_given else "<literal>"
                ),
                lineterm="",
            )
        )
        first_envelope = _diff_page_envelope(first)
        first_envelope.update(
            _name_resolution_payload(name, resolved_name)
        )
        other_envelope = (
            _diff_page_envelope(second)
            if other_name_given else None
        )
        if other_envelope is not None and resolved_other_name is not None:
            other_envelope.update(
                _name_resolution_payload(
                    other_name, resolved_other_name
                )
            )
        return {
            "diff": diff,
            "name": first_envelope,
            "other": other_envelope,
        }

    @mcp.tool(
        title="List tasks",
        description=(
            "Enumerate checkbox bullets on a SilverBullet page "
            "(per-page form, always available) or across the "
            "whole space (space-walk form, requires the journal "
            "surface to be enabled via "
            "`MCP_SILVERBULLET_JOURNAL_TOOLS=1` plus "
            "`MCP_SILVERBULLET_SPACE_PATH`). Returns one entry per "
            "checkbox bullet: `{name, ref, line, state, text}`. "
            "`name` is the page the bullet lives on (path "
            "relative to the space root for the space-walk "
            "form). `ref` is the wikilink target on the same "
            "line (``[[Pages/Hobbies]]`` → ``\"Pages/Hobbies\"``; "
            "an aliased ``[[...|display]]`` strips the alias so "
            "the ref is the wikilink *target*, not the display "
            "text) or `null` when the bullet has no wikilink "
            "(such bullets are not addressable by `check_task`; "
            "use `patch_page_lines` for those). `line` is the "
            "1-indexed editor line number (frontmatter included, "
            "matching what an SB editor highlights). `state` is "
            "the literal checkbox character: `\" \"` for `[ ]` "
            "(todo), `\"x\"` for `[x]` (done), `\"X\"` for `[X]` "
            "(cancelled — SB's third state). `text` is the "
            "bullet's content after the checkbox marker. "
            "Frontmatter-block bullets are skipped (they're "
            "YAML config keys, not tasks). "
            "Pass `page` to read a single page via "
            "`GET /.fs/{name}`; omit `page` to walk the space "
            "directory directly (gated by the journal tools "
            "config; `prefix` filters filenames as a substring "
            "when walking). Errors: page not found → standard "
            "`ToolError(\"page not found: <name>\")`; omitting "
            "`page` without the journal gate on → "
            "`ToolError(\"list_tasks without page argument "
            "requires the journal surface to be enabled\")`."
        ),
    )
    async def list_tasks(
        page: str | None = None, prefix: str = ""
    ) -> list[dict[str, object]]:
        if page is not None:
            # T39: normalize the page name (strip whitespace,
            # append ``.md`` to bare names) before the SB round
            # trip. The list return shape carries ``name`` on
            # each entry (the page each bullet lives on, which is
            # the same as the page the caller asked for); the
            # agent learns the convention by reading the rows.
            resolved_page = _normalize_page_name(page)
            # Per-page form: always available because it routes
            # through ``sb_client.read_page``, which doesn't need
            # direct FS access. The same 404 / 5xx / 412 / 413
            # / timeout wording as the read tool surfaces via
            # :func:`_translate_sb_errors`.
            async with _translate_sb_errors(resolved_page):
                result = await sb_client.read_page(resolved_page)
            body = result.body or ""
            entries = _parse_tasks(resolved_page, body)
            return [
                {
                    "name": entry.name,
                    "ref": entry.ref,
                    "line": entry.line,
                    "state": entry.state,
                    "text": entry.text,
                }
                for entry in entries
            ]
        # Space-walk form: gated by ``journal_root`` (which
        # :func:`build_mcp` populates from the journal config).
        # The gate exists because the walker reads the SB space
        # directory directly, which a sidecar without a volume
        # mount cannot do — the same constraint the journal
        # gate (T10) was set up to handle.
        if journal_root is None:
            raise ToolError(
                "list_tasks without page argument requires the "
                "journal surface to be enabled "
                "(MCP_SILVERBULLET_JOURNAL_TOOLS=1 plus "
                "MCP_SILVERBULLET_SPACE_PATH)"
            )
        return await _list_tasks_for_space(journal_root, prefix)

    @mcp.tool(
        title="Check task",
        description=(
            "Flip a checkbox bullet's state by its wikilink ref. "
            "Locates the unique checkbox bullet on `page` whose "
            "wikilink target equals `ref` (case-sensitive, "
            "matching `list_tasks` and SB's case-sensitive page "
            "lookup) and flips the `[ ]` / `[x]` / `[X]` marker "
            "to the requested `state`. `state=\"done\"` "
            "(default) flips to `[x]`, `state=\"todo\"` flips to "
            "`[ ]`, `state=\"cancelled\"` flips to `[X]` "
            "(SB's third state). Any other value is "
            "`ToolError(\"state must be one of: done, todo, "
            "cancelled\")` — surfaced upfront, no read round "
            "trip. The rest of the line (leading whitespace, the "
            "dash, the bullet text, the wikilink itself) is "
            "preserved verbatim — only the character inside the "
            "square brackets changes. Returns the T23 write "
            "acknowledgement `{name, etag, size_bytes, "
            "last_modified_ms, created_ms}` (the etag / size / "
            "timestamps reflect what was actually written, not "
            "what was read — same carry-forward as the other "
            "read-modify-write tools). `if_match=\"*\"` requires "
            "the page to exist; `if_match=<etag>` requires the "
            "body hash to match the read's etag (same "
            "read-modify-write contract as `append_to_page` / "
            "`patch_page_lines` / `patch_page_replace` — "
            "concurrent edits fail with the unified 412 "
            "ToolError rather than silently clobbering). "
            "`dry_run=True` (T26) returns `{dry_run: True, "
            "original: str, patched: str, diff: str}` without "
            "writing — the read still happens, the in-memory "
            "flip is computed, `if_match=<etag>` is checked "
            "against the read's etag (a stale etag raises "
            "412-equivalent ToolError so the agent sees one "
            "shape across both paths), and the tool reports "
            "back the line that would have changed. The "
            "pre-read input-validation errors below still fire "
            "on dry-run — a caller that passes an unknown "
            "`state` or an empty `ref` gets the same specific "
            "ToolError the live path would surface, not a vague "
            "preview. Errors: empty `ref` upfront → "
            "`ToolError(\"ref must not be empty\")`; a missing "
            "page → standard `ToolError(\"page not found: "
            "{page}\")`; no bullet on the page has a wikilink "
            "matching `ref` → "
            "`ToolError(\"no task with ref {ref} on page {page}; "
            "the task may not have a wikilink ref or may live "
            "on a different page\")`; multiple bullets have "
            "matching wikilinks → "
            "`ToolError(\"ref {ref} matches multiple tasks on "
            "page {page}; narrow the ref or use "
            "patch_page_lines directly\")` (the multi-match "
            "case is a caller error — two same-refed tasks on "
            "one page is the rare edge that needs "
            "disambiguation, not silent toggling of the first "
            "one); stale etag → standard 412 ToolError."
        ),
    )
    async def check_task(
        page: str,
        ref: str,
        state: str = "done",
        if_match: str | None = None,
        dry_run: bool = False,
        max_retries: int = 3,
    ) -> dict[str, object]:


        # T47: thread the entire read-modify-write block
        # through the auto-retry helper. The closure
        # re-reads on each iteration so the bullet-flip
        # lookup runs against the page's *current* state.
        async def attempt() -> dict[str, object]:
            # Cheap, no-read input validation first. An unknown
            # `state` is almost certainly a caller typo
            # (``\"checked\"``, ``\"complete\"``, …) — surface it
            # loudly upfront so the read-modify-write round trip
            # isn't wasted. An empty `ref` would match every line
            # containing ``[[]]`` — a degenerate intent that
            # ``_find_task_bullet` already treats as "no match"
            # (``\"\"` is a caller bug, not a real lookup"), but
            # surfacing it upfront gives the agent a clearer
            # failure than \"no task with ref on page\". Mirrors
            # the upfront guards on the other read-modify-write
            # tools (``append_to_page`'s empty-text,
            # ``patch_page_replace`'s empty-find). T40: shared
            # helper threaded here for wording consistency.
            _validate_nonempty_value(ref, label="ref")
            target_marker = _validate_check_task_state(state)
            # T39: normalize the page name (strip whitespace, append
            # ``.md`` to bare names) before the SB round trip. The
            # ``name_resolution`` field on the response envelope
            # (live or dry-run) tells the agent what the bridge
            # changed so it can learn the convention for its next
            # call. The ``ref`` argument is a wikilink target, not a
            # page name — leave it alone; the existing
            # :func:`mcp_silverbullet.journal._normalize_link_target`
            # canonicalization handles wikilink lookup.
            resolved_page = _normalize_page_name(page)
            async with _translate_sb_errors(resolved_page):
                result_page = await sb_client.read_page(resolved_page)
            # T36: cap the about-to-be-written body before the PUT.
            # The cap applies to the post-shaping body (the page
            # with the bullet flipped), which is what the PUT will
            # actually carry. ``check_task`` on a > 256 KiB page
            # would hit the cap — unusual (the cap is 256 KiB), but
            # the bridge surfaces the clear ``body too large``
            # ``ToolError`` rather than a deferred failure at SB.
            # Note: the *read* step above is unaffected by the cap
            # (the T36 ticket's "a 500 KB existing page is fine" rule
            # is about the read step being allowed; the write is
            # what's capped).
            body = result_page.body or ""
            # Distinct error surfaces for 0-match vs multi-match.
            # ``_find_task_bullet` returns the *first* match without
            # counting; we re-walk to count explicitly so the
            # wording carries the count the agent needs to fix the
            # call (\"narrow the ref or pass replace_all…\"
            # equivalent for tasks).
            match = _find_task_bullet(body, ref)
            if match is None:
                raise ToolError(
                    f"no task with ref {ref} on page {resolved_page}; "
                    f"the task may not have a wikilink ref or may "
                    f"live on a different page"
                )
            # Count additional matches by re-parsing — the
            # ``_parse_tasks`` walker is O(page size) and we just
            # spent one read, so this is cheap and the agent sees
            # the count in the error wording rather than having
            # to debug a \"succeeded for the wrong bullet\"
            # outcome.
            all_tasks = _parse_tasks(resolved_page, body)
            match_count = sum(1 for t in all_tasks if t.ref == ref)
            if match_count > 1:
                raise ToolError(
                    f"ref {ref} matches multiple tasks on page "
                    f"{resolved_page}; narrow the ref or use "
                    f"patch_page_lines directly"
                )
            # Single match confirmed. ``_apply_checkbox_flip`
            # splices the modified line into a new body via the
            # byte offsets ``_find_task_bullet` returned; the rest
            # of the page is byte-exact (same trailing-newline
            # shape, no implicit changes outside the bullet line).
            flip_result = _apply_checkbox_flip(body, ref, state)
            if flip_result is None:
                # Defence-in-depth: ``_find_task_bullet` already
                # matched, but ``_apply_checkbox_flip` re-runs it
                # under the hood and could conceivably see a
                # different answer if the body mutated between
                # calls (it can't here — both run synchronously
                # inside the ``async with`` block — but raising a
                # clearer error beats a ``None``-attribute
                # surprise).
                raise ToolError(
                    f"no task with ref {ref} on page {resolved_page}; "
                    f"the task may not have a wikilink ref or may "
                    f"live on a different page"
                )
            new_body, editor_line, _original_state, _new_state = flip_result
            # T36: cap the about-to-be-written body. We compute the
            # cap on ``new_body`` (the post-shaping body that the
            # PUT will carry) — ``check_task`` is a single-character
            # edit, so the post-shaping body is roughly the same
            # size as the pre-shaping body; a > 256 KiB page hit by
            # ``check_task`` is unusual but the cap is the same
            # uniform guardrail the rest of the write surface uses.
            _check_body_size(new_body)
            if dry_run:
                # T26: validate ``if_match`` against the read's etag
                # *here* because no PUT happens. Same shape as the
                # other patch tools' dry-run paths.
                _validate_if_match_on_read(result_page.etag, if_match)
                payload = _dry_run_payload(body, new_body)
                payload.update(
                    _name_resolution_payload(page, resolved_page)
                )
                return payload

            # Live path: thread the read's etag into the write so a
            # concurrent edit fails 412 rather than silently
            # clobbering. The read carries no precondition (matches
            # ``append_to_page` / ``patch_page_*` siblings); the
            # write's ``If-Match`` is the *caller's* guard, lifted
            # to the bridge-side read so the etag-threading is
            # automatic and the caller doesn't have to repeat it.
            # An explicit ``if_match`` from the caller is honored
            # verbatim — if the caller passed a *stale* etag
            # themselves, the write fails 412 at SB (or
            # ``_translate_sb_errors` translates that to the
            # unified ToolError wording). The only case where the
            # bridge overrides the caller's ``if_match`` is the
            # ``None`` case, where we thread the read's etag
            # through so concurrent edits are caught without the
            # caller having to manage an etag round-trip.
            write_if_match = (
                if_match if if_match is not None else result_page.etag
            )
            async with _translate_sb_errors(resolved_page):
                meta = await sb_client.write_page(
                    resolved_page, new_body, if_match=write_if_match
                )
            # T31b: same post-write concurrency-token verification
            # as the other read-modify-write tools. ``write_if_match``
            # carries the auto-threaded read etag (or the caller's
            # explicit ``if_match`` when set), so the helper
            # compares against the same value that threaded through
            # ``If-Match``. On SBs that don't honor ``If-Match``, a
            # concurrent edit between read and write fails the
            # helper's etag compare and surfaces as the unified
            # ``concurrent edit detected`` ``ToolError``.
            await _verify_concurrency_token(
                sb_client,
                resolved_page,
                post_write_meta=meta,
                expected_etag=write_if_match,
                dry_run=dry_run,
            )
            payload = _write_meta_to_payload(meta)
            payload.update(_name_resolution_payload(page, resolved_page))
            return payload

        return await _auto_retry_on_concurrent_edit(
            attempt, max_retries=max_retries
        )

    @mcp.resource(
        "silverbullet://page/{name}",
        name="silverbullet_page",
        title="SilverBullet page",
        description=(
            "Markdown body and metadata of a SilverBullet page, for "
            "attaching to conversation context. Returns `{body, "
            "etag, size_bytes, last_modified_ms}` as a JSON object "
            "(T24 — same shape as the read tool; `name` and "
            "`created_ms` are dropped for the same reasons as the "
            "tool). MIME type is `application/json` because the "
            "returned value is a structured envelope, not raw "
            "markdown — callers that want the body as a string read "
            "`contents[0].text` (a JSON-serialized object) and "
            "extract `body` themselves. The full envelope lets "
            "callers chain off `etag` for `if_match` round-trips "
            "without a second tool call."
        ),
        mime_type="application/json",
    )
    async def silverbullet_page(name: str) -> dict[str, object]:
        # T39: normalize the name (strip whitespace, append
        # ``.md`` to bare names) before the SB round trip. The
        # ``name_resolution`` field on the response envelope
        # tells the agent what the bridge changed so it can
        # learn the convention for its next call. Same shape as
        # the read tool's ``name_resolution`` envelope.
        resolved_name = _normalize_page_name(name)
        try:
            page = await sb_client.read_page(resolved_name)
        except PageNotFound as exc:
            # 404 is a ResourceNotFoundError per the SDK's two-shape
            # split: ``-32602 invalid params`` for "doesn't exist"
            # (SEP-2164), ``-32603 internal error`` for everything
            # else. ToolError would be wrong here — tools use it to
            # set ``is_error=True`` on a successful call, but
            # ``resources/read`` errors come back as JSON-RPC errors
            # and Grok's connector treats both shapes identically.
            # The error surfaces the *resolved* name (the canonical
            # form the bridge tried) so the agent sees the same
            # name it passed in (if the input was already
            # canonical) or the normalized form (if T39 added a
            # suffix) — consistent with the success-path envelope.
            raise ResourceNotFoundError(
                f"page not found: {resolved_name}"
            ) from exc
        except ServerError as exc:
            raise ResourceError(str(exc)) from exc
        except httpx.TimeoutException as exc:
            raise ResourceError("silverbullet request timed out") from exc
        payload = _read_meta_to_payload(page)
        payload.update(_name_resolution_payload(name, resolved_name))
        return payload


__all__ = [
    "build_mcp",
    "register_tools",
    "FileMeta",
    "PageMeta",
    "SBError",
    "JournalConfig",
]

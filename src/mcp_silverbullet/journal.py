"""Journal surface — direct-FS read tools, gated by config.

T10 of the map gates the journal surface; T11 implements three of the
four tools (``journal_histogram``, ``tag_summary``,
``recent_pages``); T12 implements the fourth (``pages_touching_topic``).
T29 adds the bullet-primitive parser (and the space-walk variant of
``list_tasks``); T30 adds ``check_task`` — read before write,
flip the checkbox by wikilink ref. T34 of v1.3 adds ``search_pages``
— a bounded wrapper over T12's machinery with a ``limit`` knob
(default 20, hard cap 100). The bridge may run on a host that
does *not* have direct access to the SB space directory (e.g., a
sidecar container without a volume mount); the journal tools are an
optional, strictly-additive surface that requires
``MCP_SILVERBULLET_SPACE_PATH`` and ``MCP_SILVERBULLET_JOURNAL_TOOLS``
to enable. With either unset or the path unreadable, the bridge boots
cleanly without the journal tools and the existing ``/.fs``-backed
tools continue to work.

The T29/T30 bullet primitives have a split gate:

- The per-page form of :func:`list_tasks` and :func:`check_task`
  route through ``sb_client.read_page`` / ``write_page`` and are
  *always* available — the bridge can read any page it has access
  to regardless of the space-path mount, because ``/.fs/{name}``
  doesn't need the local FS. (This is the same reason a
  per-page form is meaningful at all: SB's HTTP API is reachable
  on a sidecar without a volume mount, so per-page reads work
  even when the space-walk tools are off.)
- The space-walk form of :func:`list_tasks` (``page`` omitted,
  ``prefix`` filters filenames) walks ``space_root`` directly and
  requires the journal gate (the same T10 gate, no new env vars).

Two-step gate (resolved at :func:`resolve_journal_config`):

1. ``MCP_SILVERBULLET_JOURNAL_TOOLS`` is truthy — otherwise the gate
   is off and we skip every other check (operator did not opt in).
2. ``MCP_SILVERBULLET_SPACE_PATH`` is a non-empty path AND
   ``os.access(path, os.R_OK)`` — otherwise the gate is off and we
   log a one-line WARN.

All four tools walk the space directory directly:

- :func:`_iter_md` restricts to ``*.md`` files (top level and below),
  skips hidden directories (``*.cache``, ``.git``, ``.ssh`` — the
  space layout on this dev box carries those), and validates the
  optional ``prefix`` against path-traversal attempts (``..`` or a
  leading ``/`` raise ``ToolError`` before any FS call).
- :func:`_parse_tags` reads a small subset of YAML frontmatter
  (``---\\n...\\n---\\n``) and extracts ``tags:`` as either a scalar
  string or a YAML block-list. No PyYAML dependency: the shapes SB
  pages emit in the wild are bounded (``tags: scalar`` or
  ``tags:\\n  - foo\\n  - bar``), and a hand-rolled parser keeps the
  dep list aligned with the standing preferences (off-the-shelf
  libraries only).
- :func:`_bucket_key` prefers the daily-journal filename convention
  (``YYYY-MM-DD.md``) and falls back to the file's mtime when the
  filename doesn't carry a date.
- :func:`_pages_touching_topic` (T12) reads every file's body for the
  snippet anyway, so the ``rg --json`` path (T12, optional
  acceleration) only saves body reads for files that don't have a
  content match.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.mcpserver.server import MCPServer


_log = logging.getLogger(__name__)


# Truthy parse for ``MCP_SILVERBULLET_JOURNAL_TOOLS``. Mirrors the
# ``str_to_bool`` convention used by many CLIs: 1 / true / yes / on
# (any case) enable, everything else disables. Explicit "0" / "false"
# are unambiguous; an empty string disables (so an unset var disables
# even if the operator sets the space path).
def _is_truthy(raw: str) -> bool:
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class JournalConfig:
    """The two env-var-derived knobs the journal gate turns on.

    Attributes
    ----------
    enabled
        Operator opted in (truthy ``MCP_SILVERBULLET_JOURNAL_TOOLS``)
        AND ``space_path`` is set and readable.
    space_path
        Absolute path to the SB space directory; ``None`` when the
        operator did not set ``MCP_SILVERBULLET_SPACE_PATH`` or the
        gate is off.
    """

    enabled: bool
    space_path: str | None


def resolve_journal_config(environ: dict[str, str]) -> JournalConfig:
    """Read the two env vars; return whether the gate is on.

    Logs ``INFO`` once when the gate opens (so an operator watching
    boot logs sees the journal surface attached) and ``WARN`` once
    when it is requested-but-unusable (opt-in without a readable
    path). The log lines are intentionally single-line so they don't
    bury the rest of the boot trace.
    """
    opted_in = _is_truthy(environ.get("MCP_SILVERBULLET_JOURNAL_TOOLS", ""))
    space_path = (environ.get("MCP_SILVERBULLET_SPACE_PATH") or "").strip() or None
    if not opted_in:
        return JournalConfig(enabled=False, space_path=None)
    if space_path is None:
        _log.warning(
            "journal tools disabled: MCP_SILVERBULLET_JOURNAL_TOOLS is set "
            "but MCP_SILVERBULLET_SPACE_PATH is empty"
        )
        return JournalConfig(enabled=False, space_path=None)
    if not os.access(space_path, os.R_OK):
        _log.warning(
            "journal tools disabled: MCP_SILVERBULLET_SPACE_PATH=%s is "
            "not readable", space_path
        )
        return JournalConfig(enabled=False, space_path=space_path)
    _log.info(
        "journal tools enabled: MCP_SILVERBULLET_SPACE_PATH=%s", space_path
    )
    return JournalConfig(enabled=True, space_path=space_path)


# --- T11 read-tool internals -------------------------------------------


@dataclass(frozen=True)
class PageRef:
    """One ``*.md`` page under the SB space directory.

    ``mtime_iso`` is the file's last-modified time formatted as an
    ISO-8601 string (``YYYY-MM-DDTHH:MM:SS+00:00``); ``size_bytes``
    is the byte length of the file on disk (UTF-8 for ``*.md``). The
    name is the path relative to the space root, using forward
    slashes regardless of host OS so the tool's output is portable.
    """

    name: str
    mtime_iso: str
    size_bytes: int


# Daily-journal filename convention: ``YYYY-MM-DD.md`` (SB's default
# daily-note plugin stamps this). Captured as the prefix only, so
# ``2023-10-05.md`` matches and ``2023-10-05-evening.md`` does too
# (the latter will fall back to mtime for the bucket key).
_DAILY_DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})")


# --- T29 / T30 bullet-primitive internals ------------------------------


# Mapping from T30's ``state`` argument to the literal checkbox
# character. Lives at module scope so the parser, the MCP tool
# handler, and the tests all read from one source of truth — a
# future SB editor change (e.g. a fourth state for "blocked") is
# a one-line edit here plus the corresponding ``ToolError`` list
# in :func:`_validate_check_task_state`.
_STATE_TO_MARKER: dict[str, str] = {
    "todo": " ",
    "done": "x",
    "cancelled": "X",
}


def _validate_check_task_state(state: str) -> str:
    """Return the checkbox character for ``state`` or raise ``ToolError``.

    The T30 ticket narrowed the public surface to three states
    (``"done"`` / ``"todo"`` / ``"cancelled"``) so the agent
    doesn't need to remember whether it's ``"x"`` / ``"X"`` /
    ``" "`` (the on-disk characters) or ``"done"`` / ``"cancelled"``
    / ``"todo"`` (the action names). The mapping lives in
    :data:`_STATE_TO_MARKER`; this helper is the single error
    surface so the validation wording stays in one place — a future
    state addition (e.g. ``"blocked"``) updates both the dict and
    this function's allowed-set string in one commit.

    Raises :exc:`mcp.server.mcpserver.exceptions.ToolError` (a
    design-doc-style error, surfaced to the agent via ``is_error=True``)
    when ``state`` is not one of the three allowed names.
    """
    if state not in _STATE_TO_MARKER:
        raise ToolError(
            f"state must be one of: {', '.join(_STATE_TO_MARKER)}"
        )
    return _STATE_TO_MARKER[state]


def _apply_checkbox_flip(
    body: str, ref: str, target_state: str
) -> tuple[str, int, str, str]:
    """Flip the checkbox of the unique bullet whose wikilink ref matches.

    Returns ``(new_body, editor_line, original_state, new_state)``
    on a unique match, where ``new_body`` is the patched body
    (same trailing-newline shape as the input — no implicit
    newline added or removed). The byte offsets
    :func:`_find_task_bullet` returns are spliced into a new body
    so the rest of the page is byte-exact: a body like
    ``"header\\n- [ ] task [[Ref]] ref\\n"`` spliced with
    ``"REPLACED"`` for the bullet line produces
    ``"header\\nREPLACED\\n"`` — same shape, only the bullet line
    touched.

    The four-character marker (``- [ ]`` → ``- [x]`` etc.) is the
    only part of the line that changes; the rest of the line
    (leading whitespace, the dash, the trailing text, the wikilink)
    is preserved verbatim. This matches what the SB editor does on
    a task-state click and keeps the byte-equal property the
    caller relies on for ``if_match`` round-trips (the etag from
    the underlying ``write_page`` reflects exactly the bytes the
    bridge just wrote, with no surprise line-ending changes).

    Returns ``None`` (sentinel) when no bullet matches ``ref`` —
    the caller (:func:`mcp_silverbullet.server.check_task`)
    surfaces a clearer ``ToolError("no task with ref …")` rather
    than relying on the ``None`` to distinguish missing-ref from
    any other failure mode. A multi-match is signalled the same
    way; the caller's job is to *count* matches before flipping.

    Parameters
    ----------
    body
        The page body the tool just read.
    ref
        Wikilink target to match (case-sensitive; matches
        :func:`_find_task_bullet`'s contract). An empty ref
        matches no bullet (``_find_task_bullet` treats it as a
        caller bug) — we surface the same ``None`` so the
        caller's error wording is uniform.
    target_state
        One of the keys in :data:`_STATE_TO_MARKER`. The caller
        is expected to have validated this *before* calling
        :func:`_apply_checkbox_flip` (the MCP tool handler does
        so via :func:`_validate_check_task_state`) — passing an
        unknown state here would silently produce a broken marker
        line.
    """
    match = _find_task_bullet(body, ref)
    if match is None:
        return None  # type: ignore[return-value]
    editor_line, original_state, _text, start, end = match
    new_marker = _STATE_TO_MARKER[target_state]
    # The line is ``body[start:end]`` (byte-exact, no newline
    # attached). Rebuild the marker prefix ``"  - [<state>] "`` by
    # substituting the state character at the right offset; the
    # regex :data:`_TASK_BULLET_RE` matched a marker at column 0
    # plus optional leading whitespace (``(\\s*)-``), so the
    # ``[`` is at the same offset every time. We don't try to
    # recompute the prefix — we splice just the *character* at the
    # bracket slot, which leaves the surrounding text alone.
    #
    # The marker slot inside ``body[start:end]`` is the 4th
    # character (``"  - "`` then ``"["``); leading-whitespace
    # length is whatever the regex captured (``_TASK_BULLET_RE``'s
    # group 1). Easiest path: re-apply the regex to the
    # single-line text and rebuild with the new state. That's
    # still O(line length) and perfectly readable.
    raw_line = body[start:end]
    bullet_match = _TASK_BULLET_RE.match(raw_line)
    assert bullet_match is not None, (
        "_find_task_bullet returned a match for a line that doesn't "
        "match _TASK_BULLET_RE — internal invariant broken"
    )
    leading = bullet_match.group(1)
    _text2 = bullet_match.group(3)
    new_line = f"{leading}- [{new_marker}] {_text2}"
    new_body = body[:start] + new_line + body[end:]
    return new_body, editor_line, original_state, new_marker




@dataclass(frozen=True)
class TaskEntry:
    """One checkbox bullet parsed from a SilverBullet page.

    ``name`` is the page the bullet lives on (the SB path-relative
    name; same shape :class:`PageRef.name` uses, so callers can
    thread it through the same display code). ``line`` is the
    1-indexed line number on the page (matches what an editor
    displays — line 1 is the first line of the file, even when the
    file has no frontmatter, and the ``+1`` for frontmatter is
    intentionally NOT applied: SB's editor counts lines including
    the frontmatter block, so a bullet on what looks like
    "line 8 of the body" is line 9 if there's a 3-line
    frontmatter). ``state`` is the literal character inside the
    brackets: ``" "`` for ``[ ]`` (todo), ``"x"`` for ``[x]`` (done),
    ``"X"`` for ``[X]`` (cancelled — SB's third state).

    ``ref`` is the *wikilink target* on the same line (the text
    inside the first ``[[...]]`` token, stripped of an optional
    ``|alias`` suffix), or ``None`` when the bullet has no
    wikilink. A ``None`` ref means the bullet is *not addressable*
    by T30's :func:`check_task` — the agent falls back to
    :func:`patch_page_lines` for non-wikilinked bullets, per the
    v1.2 standing preference "Auto-migrate bullets to add a
    synthetic wikilink is explicitly out of scope: destructive,
    the user didn't ask for it, and it would change the meaning
    of existing pages."

    ``text`` is the bullet's text after the ``[ ]`` marker,
    leading whitespace trimmed. The wikilink (if any) is left in
    place so the agent can read both the ref and the surrounding
    prose.
    """

    name: str
    line: int
    state: str
    ref: str | None
    text: str


# Bullet pattern: optional leading whitespace (so nested bullets
# match), ``-`` followed by at least one space, ``[`` then exactly
# one of `` `` / ``x`` / ``X`` then ``]`` then at least one space,
# then the rest of the line. ``re.match`` anchors at column 0 so
# leading whitespace is captured by ``\s*``. We deliberately do NOT
# allow ``*`` or ``+`` markers (SB's editor renders those as plain
# bullets, not tasks); we don't allow ``[X]``-vs-``[x]`` confusion
# (case matters — ``[X]`` is the cancelled state, ``[x]`` is done;
# a lowercase ``[X]``-style mix-up would be a SB editor bug, not a
# page-author choice).
_TASK_BULLET_RE = re.compile(r"^(\s*)-\s+\[([ xX])\]\s+(.*)$")


# Wikilink pattern: ``[[<target>]]`` where ``<target>`` may contain
# ``|`` for an alias (we strip the alias below — the agent needs
# the *target*, not the *display text*). We don't try to handle
# nested ``[[ ]]`` (Markdown doesn't allow them inside a single
# wikilink token); the first ``]]`` closes the wikilink.
_WIKILINK_RE = re.compile(r"\[\[([^\]\[]*?)\]\]")


def _split_frontmatter_lines(body: str) -> tuple[list[str] | None, list[str]]:
    """Return ``(frontmatter_lines_or_None, body_lines)`` for a page body.

    The shape matches SB's frontmatter convention: ``---\\n…
    \\n---\\n<body>``. The returned ``body_lines`` excludes the
    closing fence and the trailing newline; ``frontmatter_lines``
    is everything between the two fences, or ``None`` when the
    body has no frontmatter at all (the opening ``---`` fence is
    missing).

    The ``None`` shape distinguishes "no frontmatter" from "empty
    frontmatter block" — both yield ``frontmatter_lines`` of
    length zero, but :func:`_parse_tasks` needs to know which is
    which to compute editor-shaped line numbers (an empty
    frontmatter block still occupies two lines — the opening and
    closing fences). :func:`_parse_tags` predates this helper and
    keeps its own frontmatter-detection logic (its regex-based
    approach distinguishes a top-level ``tags:`` key from nested
    YAML without parsing the whole frontmatter; consolidating the
    two parsers is a v1.3+ ticket, not v1.2). Callers that don't
    care about the no-frontmatter distinction can treat ``None``
    the same as ``[]`` and not break.

    When the frontmatter is malformed (opening fence but no
    closing fence) the function returns ``(None, body_lines)``
    rather than ``([], body_lines)`` — the body shape stays the
    same (the walker doesn't have to special-case a malformed
    page) and the "no frontmatter" signal is honest about the
    page being broken. Better to under-count tasks on a
    malformed page than to silently drop them.
    """
    lines = body.split("\n")
    if not lines or lines[0] != "---":
        return None, lines
    # The opening fence is ``---`` followed by a newline (the
    # ``split("\n")`` above already gives us the first ``"---"``
    # element and dropped the newline). Look for the closing fence
    # — a standalone ``---`` (no leading whitespace, per YAML spec)
    # on its own line.
    close_idx: int | None = None
    for idx in range(1, len(lines)):
        if lines[idx] == "---":
            close_idx = idx
            break
    if close_idx is None:
        # Malformed: opening fence but no closing fence. Treat as
        # if no frontmatter was present — the body shape stays the
        # same and the walker doesn't have to special-case a
        # malformed page.
        return None, lines
    return lines[1:close_idx], lines[close_idx + 1 :]


def _parse_tasks(name: str, body: str) -> list[TaskEntry]:
    """Extract checkbox bullets from a SilverBullet page body.

    Returns one :class:`TaskEntry` per matched bullet, in source
    order. Line numbers are 1-indexed against the **whole** body
    (frontmatter included) — matches what an SB editor displays,
    so the agent's ``patch_page_lines(name, line=8, …)`` can
    target the same line the editor would highlight.

    Bullets inside the frontmatter block are skipped (they're
    YAML config keys, not tasks; matching them would surface
    ``---``-fenced YAML as a task list and confuse the agent).
    Nested bullets at any indentation level are matched — SB's
    editor treats nested ``- [ ]`` lines as addressable tasks, so
    the bridge does too. Code-block-fenced bullets (``- [ ]``
    inside a ```` ``` ```` block) are *not* specially skipped —
    a v1.2 limitation; documented in the v1.2 map's T29 ticket
    as a known gap. The next line-number / wikilink-aware task
    primitive in v1.3 should add code-block awareness.

    The wikilink extraction picks the *first* ``[[…]]`` on the
    line (the one closest to the marker) and strips any ``|alias``
    suffix — the editor's ``externalTaskRef`` resolves to the
    wikilink *target*, not the display text. Multi-wikilink lines
    (rare in the wild) keep the first ref; the agent can still
    read the rest of the bullet text via :attr:`TaskEntry.text`
    if it needs the second ref.
    """
    frontmatter_lines, body_lines = _split_frontmatter_lines(body)
    # Editor-shaped line numbers count from 1 against the *full*
    # body (frontmatter included). When there is no frontmatter
    # (``frontmatter_lines is None``) the offset is 1 (body-lines
    # start at editor line 1); when frontmatter is present, the
    # offset is ``N + 3`` where ``N`` is the number of frontmatter
    # content lines (opening fence = line 1, ``N`` content lines,
    # closing fence = line ``N + 2``, body starts at line ``N + 3``).
    if frontmatter_lines is None:
        frontmatter_offset = 1
    else:
        frontmatter_offset = len(frontmatter_lines) + 3
    tasks: list[TaskEntry] = []
    for line_idx, raw in enumerate(body_lines):
        match = _TASK_BULLET_RE.match(raw)
        if match is None:
            continue
        # ``line_idx`` is 0-indexed into ``body_lines`` (post-
        # frontmatter). The ``frontmatter_offset`` shifts it to the
        # editor-shaped 1-indexed line directly (no further ``+1``
        # needed — the offset already accounts for the 1-indexed
        # convention).
        editor_line = line_idx + frontmatter_offset
        state = match.group(2)
        text = match.group(3).strip()
        ref = _extract_first_wikilink(text)
        tasks.append(
            TaskEntry(
                name=name,
                line=editor_line,
                state=state,
                ref=ref,
                text=text,
            )
        )
    return tasks


def _extract_first_wikilink(text: str) -> str | None:
    """Return the target of the first ``[[wikilink]]`` on the line, or ``None``.

    The target is the text inside ``[[ ]]`` with an optional
    ``|alias`` suffix stripped — the editor's ``externalTaskRef``
    resolves to the *target*, not the display text. So
    ``[[Pages/Hobbies#card|read the card]]`` yields
    ``"Pages/Hobbies#card"``, not the alias.

    The capture group is a *lazy* negated-class match (any
    character that's not ``]`` or ``[``), so the regex stops at
    the *first* ``]]`` rather than at the end of the string —
    ``[[a]] [[b]]`` yields ``"a"``, not ``"a]] [[b"``. The lazy
    match also means a stray ``]`` inside the target (rare, but
    seen on URLs that contain brackets) wouldn't break the
    parse: ``[[https://example.com/path]a]]`` is already
    malformed Markdown; we let the line fall through to a ref
    of the substring before the first ``]]``, which is the most
    useful answer in practice.
    """
    match = _WIKILINK_RE.search(text)
    if match is None:
        return None
    target = match.group(1)
    # Strip the alias: ``Pages/Hobbies#card|read the card`` →
    # ``Pages/Hobbies#card``. The pipe is the alias separator in
    # SB wikilinks (Markdown convention).
    pipe_idx = target.find("|")
    if pipe_idx >= 0:
        target = target[:pipe_idx]
    return target or None


def _find_task_bullet(
    body: str, ref: str
) -> tuple[int, str, str, int, int] | None:
    """Locate the unique checkbox bullet whose wikilink target equals ``ref``.

    Returns ``(editor_line, state, text, byte_offset, byte_end)`` on
    a unique match, or ``None`` when no bullet matches. The byte
    offsets are the (start, end) of the bullet line within the
    body — useful for splicing a new body without having to
    re-walk lines. The caller (T30's :func:`check_task`) needs
    both the line and the byte range because flipping a marker
    only changes a single character inside the line; we
    reconstruct the new body by slicing ``body[:byte_offset] +
    modified_line + body[byte_end:]``.

    Matching rule: a bullet matches iff its first ``[[wikilink]]``
    target equals ``ref`` exactly. The match is case-sensitive
    (``Pages/Hobbies`` ≠ ``pages/hobbies``) — SB's page lookup is
    case-sensitive at the file system level, so a case-folded
    match would let ``check_task`` toggle a task the editor
    couldn't find by the same ref.

    Returns ``None`` when no bullet matches. A separate ``"found
    more than one"`` signal lives in :func:`check_task` itself
    (a multi-match is a caller error, not a normal "no match"
    case), so this helper stays at "find one or report none".
    """
    if not ref:
        # An empty ref would match every line containing ``[[]]`` —
        # an empty wikilink pair — and is almost certainly a caller
        # bug, not a real "look up no task" intent. Surface it as
        # "no match" so the caller can raise the same error it would
        # surface for any other missing ref.
        return None
    # ``body.split("\n")`` returns a trailing empty string when the
    # body ends with ``"\n"`` (because ``"\n"`` is the separator, not
    # the terminator — SB stores text with a final newline the way
    # editors do). Drop the trailing empty so the iteration matches
    # the editor's "N lines" view (mirrors
    # :func:`mcp_silverbullet.server._split_body_lines`).
    body_lines = body.split("\n")
    if body_lines and body_lines[-1] == "":
        body_lines.pop()
    if not body_lines:
        return None
    matches: list[tuple[int, str, str, int, int]] = []
    byte_cursor = 0
    for editor_idx, raw in enumerate(body_lines):
        # Each line in the body is ``raw + "\n"`` (one byte) for
        # every line except the last, which has no trailing
        # newline (we already dropped the empty trailing element).
        # The byte offsets let the caller splice the modified
        # line back into the body without re-walking.
        line_byte_len = len(raw.encode("utf-8"))
        newline_byte_len = (
            len(b"\n") if editor_idx < len(body_lines) - 1 else 0
        )
        match = _TASK_BULLET_RE.match(raw)
        if match is not None:
            bullet_text = match.group(3).strip()
            target = _extract_first_wikilink(bullet_text)
            if target == ref:
                matches.append(
                    (
                        editor_idx + 1,
                        match.group(2),
                        bullet_text,
                        byte_cursor,
                        byte_cursor + line_byte_len,
                    )
                )
        byte_cursor += line_byte_len + newline_byte_len
    if len(matches) == 0:
        return None
    # Whether the match count is 1 or >1 we return the *first*
    # match so the byte offsets splice the first occurrence into
    # the new body. The caller is responsible for raising the
    # multi-match error — this helper's contract is "find the
    # first match or report none". (We use a sentinel return shape
    # rather than raising here so :func:`check_task` can build a
    # clearer error message that includes the count and the line
    # of the second match.)
    return matches[0]


def _validate_prefix(prefix: str) -> str:
    """Reject path-traversal attempts and return the prefix unchanged.

    An empty prefix is allowed and means "walk the whole space". A
    non-empty prefix must not start with ``/`` (absolute paths would
    leave the space root) and must not contain ``..`` (relative
    traversal). The prefix is a *substring* match against the file's
    relative path — ``"Daily"`` matches ``Daily/2023-10-05.md`` and
    ``Areas/Daily Notes.md``; the validation guards safety, not
    routing.
    """
    if not prefix:
        return prefix
    if prefix.startswith("/"):
        raise ToolError(
            f"journal prefix must not start with '/': {prefix!r}"
        )
    # ``..`` can appear as a standalone segment or inside a longer
    # segment (``"foo..bar"``); both are suspect. We treat any
    # occurrence as a traversal attempt.
    if ".." in prefix:
        raise ToolError(
            f"journal prefix must not contain '..': {prefix!r}"
        )
    return prefix


def _iter_md(
    space_root: Path, prefix: str
) -> Iterator[tuple[Path, str]]:
    """Yield ``(absolute_path, name_relative_to_space_root)`` for every ``*.md`` page.

    Skips hidden directories (``*.cache``, ``.git``, ``.ssh``) at
    any depth — they don't contain user-authored markdown on this
    dev box and walking them just adds noise (and would surface
    ``.git/HEAD`` etc. as broken pages if an operator queried the
    wrong prefix). Hidden files at the top level are also skipped
    for the same reason.

    The yielded ``name`` uses forward slashes on every platform so
    the tool output is portable; ``Path.as_posix()`` does the right
    thing on Windows too.
    """
    validated = _validate_prefix(prefix)
    for path in space_root.rglob("*.md"):
        # ``rglob`` includes hidden directories; filter them so we
        # don't surface ``.git/index.md`` etc.
        if any(part.startswith(".") for part in path.relative_to(space_root).parts):
            continue
        rel = path.relative_to(space_root).as_posix()
        if validated and validated not in rel:
            continue
        yield path, rel


def _bucket_key(name: str, mtime_ns: int) -> str:
    """Bucket key (``YYYY-MM``) for the histogram.

    Prefers the daily-journal filename convention; falls back to the
    file's mtime when the filename doesn't carry a date. ``name``
    is the path relative to the space root, so we look at the
    basename (``Daily/2026-01-05.md`` → ``2026-01-05.md``) rather
    than the leading directory.
    """
    basename = name.rsplit("/", 1)[-1]
    match = _DAILY_DATE_RE.match(basename)
    if match is not None:
        return match.group(1)[:7]
    # mtime_ns is a nanosecond timestamp from ``Path.stat().st_mtime_ns``.
    # Convert via UTC so a daily-journal file created in one timezone
    # doesn't accidentally bucket into the previous month.
    ts = _dt.datetime.fromtimestamp(mtime_ns / 1_000_000_000, tz=_dt.timezone.utc)
    return ts.strftime("%Y-%m")


def _parse_tags(body: str) -> list[str]:
    """Extract ``tags:`` from YAML frontmatter (scalar OR block-list).

    The hand-rolled parser covers the two shapes SB pages emit in the
    wild:

    1. ``tags: foo`` — scalar, returned as ``["foo"]``.
    2. ``tags:\\n  - foo\\n  - bar`` — block-list.

    Anything else (``tags: []``, ``tags: {a: b}``, ``tags: null``,
    no frontmatter at all, malformed YAML) yields ``[]`` rather than
    raising — the tool's job is to count tag occurrences, and we'd
    rather under-count than refuse to return. Tags are returned with
    their original case preserved (``daily`` and ``Daily`` are
    different keys; the design call is to keep them distinct so the
    operator can spot accidental casing drift).
    """
    if not body.startswith("---"):
        return []
    # Skip the opening fence (``---`` + the newline that follows it)
    # and find the closing fence (a standalone ``---`` on its own
    # line). Everything between is the frontmatter.
    after_open = body[3:]
    if not after_open.startswith("\n"):
        # ``---`` without a trailing newline is malformed YAML.
        return []
    after_open = after_open[1:]  # drop the newline after ---
    # Look for ``\n---\n`` or ``\n---\r?\n`` (closing fence on its
    # own line). ``re`` keeps it readable vs. fiddly string ops.
    m = re.search(r"\n---\r?\n", after_open)
    if m is None:
        return []
    frontmatter = after_open[: m.start()]
    lines = frontmatter.splitlines()
    # Find the ``tags:`` line. Tags must be at column 0 (top-level
    # key); indented keys are nested under another mapping and we
    # don't try to resolve them.
    for idx, raw in enumerate(lines):
        if raw.startswith("tags:"):
            value = raw[len("tags:") :].strip()
            if value:
                # ``tags: foo`` — scalar on the same line.
                return [_unquote(value)]
            # ``tags:`` with nothing on the line — look at the next
            # lines for a block-list (``  - foo`` shape).
            items: list[str] = []
            for follow in lines[idx + 1 :]:
                stripped = follow.strip()
                if not stripped.startswith("- "):
                    # Any non-list line ends the block-list. (Empty
                    # lines are skipped — SB's frontmatter sometimes
                    # has a blank line between the ``tags:`` key and
                    # its items, though this is uncommon.)
                    if stripped == "":
                        continue
                    break
                items.append(stripped[2:].strip())
            return [_unquote(item) for item in items]
    return []


def _unquote(value: str) -> str:
    """Strip a single matching pair of surrounding quotes, if present."""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


def _mtime_iso(mtime_ns: int) -> str:
    """ISO-8601 UTC timestamp (``YYYY-MM-DDTHH:MM:SS+00:00``)."""
    ts = _dt.datetime.fromtimestamp(mtime_ns / 1_000_000_000, tz=_dt.timezone.utc)
    return ts.isoformat()


# --- T12 search internals ---------------------------------------------


# Cached at module load: ``shutil.which("rg")`` is what ``rg_available``
# returns, and ``_rg_content_matches`` uses this as the absolute path.
# An empty string means "probed and not found" (so the Python fallback
# path runs without probing again). A monkeypatch to either of these
# in tests forces the Python path deterministically.
_RG_BIN: str | None = None


def _rg_available() -> bool:
    """Memoized ``shutil.which("rg")``.

    Returns False (and never re-probes) if ``rg`` isn't on PATH, so the
    per-call cost is a single attribute lookup. Tests force the Python
    fallback by setting :data:`_RG_BIN` to ``""`` directly.
    """
    global _RG_BIN
    if _RG_BIN is None:
        path = shutil.which("rg")
        _RG_BIN = path if path is not None else ""
    return bool(_RG_BIN)


# Cap on how long the optional ``rg --json`` subprocess may run before
# the bridge falls back to a pure-Python scan. ``rg`` is fast; if it
# hasn't returned in 30s on a multi-thousand-page space something is
# wrong (probably huge minified JSON in the space) and we'd rather
# fall back than hang the tool call.
_RG_TIMEOUT_S = 30.0

# Target length of a content-match snippet, in characters. The line
# containing the match is taken whole if it fits; otherwise the window
# is centered on the match and truncated with a leading or trailing
# ellipsis (``…``). Name-only matches get a body excerpt of the same
# length (no frontmatter).
_SNIPPET_MAX_LEN = 80


def _rg_content_matches(
    query: str,
    files: list[tuple[Path, str]],
) -> dict[str, str] | None:
    """Return ``{name: first_matching_line}`` via ``rg --json``, or ``None`` on failure.

    ``files`` is the (path, name) tuples from :func:`_iter_md` — the
    caller has already applied prefix filtering and hidden-dir
    skipping, so we pass each file as a positional arg rather than
    letting ``rg`` recurse the whole space.

    Returns ``None`` when ``rg`` errors or times out; the caller falls
    back to a Python substring scan over every file's body. Returns
    an empty dict when ``rg`` succeeded but found no matches (so the
    caller doesn't waste body reads).
    """
    if not _rg_available() or not files:
        return None
    assert _RG_BIN is not None  # narrowed by _rg_available
    cmd = [
        _RG_BIN,
        "--json",
        "-i",  # case-insensitive: matches the Python fallback's q.lower() in body.lower()
        "--no-config",  # don't load the user's ~/.ripgreprc — the bridge is sandboxed
        "--no-messages",  # suppress rg's stderr noise (parse errors etc.)
        "--",
        query,
    ]
    cmd.extend(str(path) for path, _name in files)
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_RG_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired:
        _log.warning(
            "rg search timed out after %.1fs; falling back to Python",
            _RG_TIMEOUT_S,
        )
        return None
    except OSError as exc:
        _log.warning("rg invocation failed: %s; falling back", exc)
        return None
    # rg exit codes: 0 = matches, 1 = no matches, 2+ = error.
    if proc.returncode not in (0, 1):
        _log.warning(
            "rg failed (exit %d): %s",
            proc.returncode,
            proc.stderr.strip().splitlines()[0] if proc.stderr else "",
        )
        return None
    abs_to_name = {str(path): name for path, name in files}
    matches: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("type") != "match":
            continue
        try:
            path_text = rec["data"]["path"]["text"]
            line_text = rec["data"]["lines"]["text"].rstrip("\n")
        except (KeyError, TypeError):
            continue
        name = abs_to_name.get(path_text)
        if name is None or name in matches:
            # Either rg returned a path we didn't pass (shouldn't
            # happen) or we already have an earlier match for this
            # file. Keep the first so the snippet is deterministic.
            continue
        matches[name] = line_text
    return matches


def _safe_read_body(path: Path) -> str | None:
    """Read the file as UTF-8; ``None`` on read or decode error."""
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _content_snippet(query_lower: str, body: str, max_len: int) -> str:
    """Snippet centered on the first case-insensitive occurrence of ``query_lower``.

    The line containing the match is taken whole when it fits in
    ``max_len``; otherwise a window of ``max_len`` chars is taken
    centered on the match within that line, with a leading or trailing
    ellipsis (``…``) when the window clips the line.
    """
    body_lower = body.lower()
    pos = body_lower.find(query_lower)
    if pos < 0:
        # Defensive: caller computed ``content_match`` against
        # ``body_lower``, so this branch only fires if there's a
        # disagreement between two calls of ``find`` on the same
        # string — fall back to a body excerpt so the caller still
        # gets *something* useful.
        return _body_excerpt(body, max_len)
    line_start = body.rfind("\n", 0, pos) + 1
    line_end = body.find("\n", pos)
    if line_end < 0:
        line_end = len(body)
    line = body[line_start:line_end].strip()
    if len(line) <= max_len:
        return line
    match_in_line = pos - line_start
    half = max_len // 2
    start = max(0, match_in_line - half)
    end = start + max_len
    if end > len(line):
        end = len(line)
        start = max(0, end - max_len)
    snippet = line[start:end].strip()
    prefix = "… " if start > 0 else ""
    suffix = " …" if end < len(line) else ""
    return f"{prefix}{snippet}{suffix}"


def _body_excerpt(body: str, max_len: int) -> str:
    """First ``max_len`` chars of the body, with YAML frontmatter stripped.

    Used for the ``name``-only match snippet — there's no content
    occurrence to center on, so we surface the page's opening prose.
    Stripping frontmatter is consistent with how :func:`_parse_tags`
    handles the same leading ``---\\n…\\n---\\n`` shape: a reader
    sees the page content, not the metadata block.
    """
    text = body
    if text.startswith("---"):
        m = re.search(r"\n---\r?\n", text[3:])
        if m is not None:
            text = text[3 + m.end():]
    text = text.strip().replace("\n", " ")
    if len(text) > max_len:
        return text[:max_len].rstrip() + "…"
    return text


def _normalize_query(query: str) -> str:
    """Strip + collapse whitespace; raise ``ToolError`` on empty input.

    Empty / whitespace-only queries would match every file under the
    space (case-insensitive substring of ``""`` is always True) — a
    confusing UX and a performance footgun. We surface it as a
    ``ToolError`` so the operator sees why the call was refused
    instead of a multi-thousand-line tool result.
    """
    normalized = " ".join(query.split())
    if not normalized:
        raise ToolError("query must not be empty")
    return normalized


# --- T11 tool bodies ---------------------------------------------------


async def _journal_histogram(
    space_root: Path, prefix: str
) -> dict[str, int]:
    """Bucket ``*.md`` files under ``space_root`` by ``YYYY-MM``.

    File-by-file traversal; safe to call against a multi-thousand-page
    space (the operator's SB has ~200 pages; a histogram over those
    completes in single-digit milliseconds).
    """
    counts: dict[str, int] = {}
    for path, name in _iter_md(space_root, prefix):
        try:
            mtime_ns = path.stat().st_mtime_ns
        except OSError:
            # The file disappeared between ``rglob`` and ``stat``
            # (concurrent editor save) — skip it rather than
            # surface an error to a read-only listing tool.
            continue
        key = _bucket_key(name, mtime_ns)
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))


async def _tag_summary(
    space_root: Path, prefix: str
) -> dict[str, int]:
    """Walk ``*.md`` files and count tag occurrences from frontmatter."""
    counts: dict[str, int] = {}
    for path, _name in _iter_md(space_root, prefix):
        try:
            body = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for tag in _parse_tags(body):
            counts[tag] = counts.get(tag, 0) + 1
    # Sort by count desc, then by tag asc so ties are deterministic.
    return dict(
        sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    )


async def _recent_pages(
    space_root: Path, *, limit: int, prefix: str
) -> list[PageRef]:
    """Return the ``limit`` most-recently-modified pages, newest first."""
    if limit <= 0:
        return []
    pages: list[tuple[int, Path, str]] = []
    for path, name in _iter_md(space_root, prefix):
        try:
            stat = path.stat()
        except OSError:
            continue
        pages.append((stat.st_mtime_ns, path, name))
    pages.sort(key=lambda item: (-item[0], item[2]))
    return [
        PageRef(
            name=name,
            mtime_iso=_mtime_iso(item_mtime_ns),
            size_bytes=item_path.stat().st_size,
        )
        for item_mtime_ns, item_path, name in pages[:limit]
    ]


# --- T12 tool body -----------------------------------------------------


async def _pages_touching_topic(
    space_root: Path, query: str, prefix: str
) -> list[dict[str, str]]:
    """Name+content substring search; returns ``[{name, match, snippet}, ...]``.

    The query is treated as a literal substring (no regex syntax; no
    ``rg`` ``--type`` heuristics). Case is folded to ASCII before
    matching on both paths. ``match`` is one of ``"name"`` (the
    relative path contains the query), ``"content"`` (the body
    contains the query), or ``"both"``. ``snippet`` is an
    ``_SNIPPET_MAX_LEN``-char window centered on the content match —
    or a body excerpt for name-only matches. Results are sorted by
    relative path so the tool output is deterministic.

    The body is read for every file with a name match OR a content
    match (we need the body for the snippet). The ``rg --json``
    acceleration saves the body read for files that have neither
    match; when ``rg`` is unavailable we read every body's body and
    substring-check in Python (still fast for ~200-page spaces).
    """
    q = _normalize_query(query)
    q_lower = q.lower()
    files = list(_iter_md(space_root, prefix))

    # When ``rg`` is on PATH, ask it which files have a content match
    # *before* reading any bodies. The call returns ``None`` on
    # ``rg`` failure (we fall through to the Python path), or a dict
    # ``{name: first_matching_line}`` on success (empty dict = no
    # matches anywhere).
    rg_matches: dict[str, str] | None = None
    if _rg_available():
        rg_matches = _rg_content_matches(q, files)

    results: list[dict[str, str]] = []
    for path, name in files:
        nm = q_lower in name.lower()
        if rg_matches is not None:
            # ``rg`` did the body filtering; trust it. We still need
            # the body for the snippet, so the body read below is
            # only skipped when *both* checks fail.
            cm = name in rg_matches
            if not (nm or cm):
                continue
            body = _safe_read_body(path)
            if body is None:
                continue
            snippet = _content_snippet(q_lower, body, _SNIPPET_MAX_LEN) if cm else _body_excerpt(body, _SNIPPET_MAX_LEN)
        else:
            # Python fallback: read every body up front to compute cm.
            # Skipping here is the only win the rg path delivers, so
            # both branches land on a body read for matched files.
            body = _safe_read_body(path)
            if body is None:
                continue
            cm = q_lower in body.lower()
            if not (nm or cm):
                continue
            snippet = _content_snippet(q_lower, body, _SNIPPET_MAX_LEN) if cm else _body_excerpt(body, _SNIPPET_MAX_LEN)

        if nm and cm:
            kind = "both"
        elif nm:
            kind = "name"
        else:
            kind = "content"
        results.append({"name": name, "match": kind, "snippet": snippet})

    # ``list[X]`` return types go through a Pydantic ``RootModel`` and
    # the wire payload is ``{"result": [...]}`` (T11 carry-forward).
    # Tests assert on that wrapping shape, not on the bare list.
    results.sort(key=lambda r: r["name"])
    return results


# --- T34 tool body (bounded search) -----------------------------------


# Default + hard cap for ``search_pages``. The default of 20 is what an
# agent typically wants ("show me the top matches for this query");
# the hard cap of 100 keeps the response bounded even for a runaway
# operator who passes ``limit=10000`` (the alternative is a multi-MB
# tool result for a single substring search, which the agent has to
# paginate through anyway).
_SEARCH_DEFAULT_LIMIT = 20
_SEARCH_HARD_LIMIT = 100


def _validate_search_limit(limit: int) -> int:
    """Reject non-positive or oversized ``limit`` before the scan.

    ``limit < 1`` would silently truncate to the empty list (the
    caller probably meant "any number of results", not "zero
    results"); ``limit > _SEARCH_HARD_LIMIT`` is a runaway-input
    guard. The hard cap is the operator's lever — operators who
    want a larger result set can fork, same as the v1.3 T36
    body-size cap.
    """
    if limit < 1:
        raise ToolError(
            f"limit must be a positive integer; got {limit}"
        )
    if limit > _SEARCH_HARD_LIMIT:
        raise ToolError(
            f"limit {limit} exceeds hard cap of "
            f"{_SEARCH_HARD_LIMIT}; narrow the query or prefix "
            f"instead of raising the cap"
        )
    return limit


async def _search_pages(
    space_root: Path,
    query: str,
    prefix: str,
    limit: int,
) -> list[dict[str, str]]:
    """Bounded substring search over the SB space directory.

    Thin wrapper over :func:`_pages_touching_topic` (T12) that
    applies the v1.3 T34 result cap. The scan, match-kind
    classification (``name`` / ``content`` / ``both``), and snippet
    shaping are all delegated to T12 — T34 is a *bounding* layer,
    not a parallel implementation, same pattern the rest of the
    v1.3 surface follows ("no new deps, reuse what v1 already
    built"). T34 differs from T12 only in:

    - A ``limit`` knob (default 20, hard cap 100) that truncates
      the post-sort list. The truncation happens *after*
      :func:`_pages_touching_topic`'s ``name``-ascending sort so
      the first ``limit`` results are the same files a caller
      would get without the cap, just with the tail chopped.
    - Tighter input validation surfaced at the tool boundary:
      :func:`_normalize_query` is called here (T12 also calls it
      inside, but a call from the tool handler makes the
      validation visible to the agent before any FS walk).

    Out of scope (deliberately):
    - BM25 / vector / semantic search — explicitly a non-goal in
      ``docs/design.md``.
    - Pagination / cursors — same v1.4+ punt as
      ``list_pages``'s pagination concern.
    - A separate ``case_sensitive`` knob — T34 inherits T12's
      case-insensitive default; a T34a follow-up can flip the
      bit if an agent ever needs it.
    """
    validated_limit = _validate_search_limit(limit)
    results = await _pages_touching_topic(space_root, query, prefix)
    return results[:validated_limit]


# --- T35 tool body (backlinks) ----------------------------------------


# T35-specific wikilink regex. The module-level ``_WIKILINK_RE`` used
# by the task-bullet helpers captures the same shape but with a *lazy*
# negated-class match that prevents the regex engine from eating
# ``[``/``]`` characters. T35's helper wants the same shape but
# surfaces *every* match on the line (not just the first), and uses a
# non-lazy class so the wire shape is well-defined when multiple
# wikilinks appear on one line (rare but seen on index pages:
# ``[[Foo]] [[Bar]] [[Baz]]``).
_BACKLINK_WIKILINK_RE = re.compile(r"\[\[([^\[\]]+?)\]\]")


def _normalize_link_target(target: str) -> str:
    """Canonicalize a wikilink target for backlink matching.

    SB accepts four ways to spell the same target:
    ``Projects/Foo``, ``Projects/Foo.md``, ``/Projects/Foo/``,
    and ``/Projects/Foo.md``. After this normalization, all four
    compare equal. The agent that queries
    ``find_backlinks("Projects/Foo")`` matches every page that
    links to any of the four spellings.

    Strip order:

    - Leading/trailing slashes (``/Projects/Foo/`` →
      ``Projects/Foo``). SB treats the leading slash as the
      space-root anchor; the canonical form has no anchor.
    - Trailing ``.md`` (``Projects/Foo.md`` →
      ``Projects/Foo``). SB stores pages as ``*.md`` on disk but
      wikilink resolution happens against the page *name*, not
      the file extension. The ``.md`` is an editor-only
      convenience.
    - Trailing whitespace. The bridge won't surface a target with
      trailing whitespace in practice, but a defensive
      ``strip()`` keeps the comparator robust against upstream
      encoding drift.
    - Empty result. A target that's empty *after* stripping (e.g.
      ``target = ".md"``) collapses to ``""``. The T35 tool
      surfaces a ``ToolError("target must not be empty")`` at
      the boundary before this helper runs, so ``""`` here is
      defense-in-depth rather than a user-visible branch.

    The normalized form is what every wikilink target on every
    line is compared against; the comparator is a plain string
    ``==`` after both sides have been normalized. Case-sensitive
    to match SB's page lookup (the v1 T6 / v1.2 T30 carry-forwards
    document this — ``find_backlinks("Foo")`` does not match
    ``[[foo]]``).
    """
    stripped = target.strip().strip("/")
    if stripped.endswith(".md"):
        stripped = stripped[: -len(".md")]
    return stripped.strip("/")


async def _find_backlinks(
    space_root: Path, target: str
) -> list[dict[str, object]]:
    """Walk every ``*.md`` page and return references to ``target``.

    Returns one entry per *line* containing at least one
    matching wikilink. The wire shape mirrors
    ``lidiaev/me-db``'s ``find_backlinks`` contract (the closest
    v1.3 competitive-landscape input):

    - ``file``: the relative path to the linking page (forward
      slashes, ``Path.as_posix()`` shape — same as T12's name
      field).
    - ``line``: 1-indexed line number (matches the
      ``pages_touching_topic`` / ``patch_page_lines``
      conventions; an editor showing line N has line N here).
    - ``text``: the stripped line text (the agent reads this
      to see *how* the page links — same line that appears in
      the editor).

    Matching rules:

    - A wikilink matches when its target (after
      :func:`_normalize_link_target`) equals the query target
      (after the same normalization). This is the only
      comparator — no fuzzy match, no alias-aware ranking.
    - Aliases (``[[target|alias]]``) match the bare ``target``
      — the alias is the *display* text, not a different page.
      The regex strips the alias before comparison; see the
      ``alias_split`` block below.
    - Self-links (``Projects/Foo`` containing
      ``[[Projects/Foo]]``) are returned as one entry. Agents
      that want to exclude self-links filter the result list
      themselves — the bridge doesn't presume.
    - Multiple matches on one line yield one entry (the line,
      not each individual match); the agent that wants
      per-match granularity calls ``rg`` themselves. Index
      pages with ``[[Foo]] [[Bar]] [[Baz]]`` return one
      ``BacklinkEntry`` per line, regardless of how many
      wikilinks the line carries.
    - No matches → ``[]`` (empty list, not a ``ToolError``).
      The agent might be querying pre-emptively ("am I about
      to break anything if I rename this page?") and a missing
      target is a legitimate answer, not a failure.

    The walker reuses :func:`_iter_md` (T11/T12) for the
    ``*.md`` enumeration + hidden-directory skip + ``prefix``
    guard. T35 doesn't take a ``prefix`` argument (the link
    graph is space-wide; scoping a backlink search by prefix
    would silently exclude cross-prefix references that the
    agent probably wants to see). A future T35a could add an
    optional ``prefix?`` knob if the use case appears.

    Wire shape: each entry is a dict with ``file`` (str),
    ``line`` (int), ``text`` (str). The MCP SDK serializes
    dicts to ``structured_content``; an agent reads the list
    via ``result["result"]``.
    """
    normalized = _normalize_link_target(target)
    if not normalized:
        # Defense-in-depth: the tool handler raises upfront,
        # but a future caller that bypasses the handler still
        # gets a sensible result.
        return []
    results: list[dict[str, object]] = []
    for path, name in _iter_md(space_root, prefix=""):
        try:
            body = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            # Skip unreadable pages silently (a binary file
            # with a ``.md`` extension, a permissions error,
            # a non-UTF-8 encoding); the walker is best-effort
            # and a single bad page shouldn't abort the scan.
            # This matches the v1 T11 / T12 walker's stance
            # on the same error class.
            continue
        for line_idx, raw_line in enumerate(body.splitlines(), start=1):
            for match in _BACKLINK_WIKILINK_RE.finditer(raw_line):
                link_target = match.group(1)
                # Strip alias: ``Foo|read the foo`` → ``Foo``.
                pipe_idx = link_target.find("|")
                if pipe_idx >= 0:
                    link_target = link_target[:pipe_idx]
                if _normalize_link_target(link_target) == normalized:
                    # One entry per matching line, not per
                    # matching wikilink (multiple matches on
                    # one line collapse to one entry). The
                    # ``break`` exits the inner ``for match``
                    # loop; the outer ``for line_idx`` loop
                    # moves on to the next line.
                    results.append(
                        {
                            "file": name,
                            "line": line_idx,
                            "text": raw_line.strip(),
                        }
                    )
                    break
    return results


# --- T29 tool body (space-walk variant) -------------------------------


async def _list_tasks_for_space(
    space_root: Path, prefix: str
) -> list[dict[str, object]]:
    """Walk every ``*.md`` page under ``space_root`` and return checkbox bullets.

    The space-walk variant of T29's :func:`list_tasks` tool. The
    per-page variant lives in :mod:`mcp_silverbullet.server` (the
    bridge reads via ``sb_client.read_page``); this walker is the
    fallback when the caller doesn't name a specific page AND the
    journal gate is on (the direct-FS access the journal tools
    rely on). Hidden directories are skipped via :func:`_iter_md`
    (same skip rule as T11/T12 — ``.git`` / ``.cache`` / ``.ssh``
    etc. don't appear in task lists).

    Sort order is ``(name, line)`` so the wire payload is
    deterministic regardless of ``os.walk`` order — the agent
    reading a space-wide task list needs a stable order to
    reason about "did I already handle this task?" between
    turns. The line numbers are editor-shaped 1-indexed against
    the full body (frontmatter included), same convention
    :func:`_parse_tasks` uses.

    Files that fail to read (encoding error, race with a
    concurrent editor save) are skipped — the same
    "read-modify-write tools should be tolerant of transient
    FS races" pattern T11's ``_recent_pages`` follows. The
    alternative is to abort the whole walk on the first
    failure, which would surface a confusing
    "list_tasks failed" to a caller that just asked for a
    space summary.
    """
    tasks: list[dict[str, object]] = []
    validated_prefix = _validate_prefix(prefix)
    for path, name in _iter_md(space_root, validated_prefix):
        try:
            body = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for entry in _parse_tasks(name, body):
            tasks.append(
                {
                    "name": entry.name,
                    "ref": entry.ref,
                    "line": entry.line,
                    "state": entry.state,
                    "text": entry.text,
                }
            )
    tasks.sort(key=lambda t: (t["name"], t["line"]))
    return tasks


# --- registration ------------------------------------------------------


def register_journal_tools(
    mcp: MCPServer,
    config: JournalConfig,
) -> None:
    """Register the six journal tools iff the gate is on.

    ``journal_histogram`` (T11) / ``tag_summary`` (T11) /
    ``recent_pages`` (T11) / ``pages_touching_topic`` (T12) /
    ``search_pages`` (T34 — bounded wrapper over T12 with a
    ``limit`` knob) / ``find_backlinks`` (T35 — wikilink-target
    backlink scan). When the gate is off, none of the six
    is registered. Called from :func:`mcp_silverbullet.server.build_mcp`; the caller
    passes the already-resolved :class:`JournalConfig` so this
    function does no env parsing and is pure against its inputs. When
    the gate is off this is a no-op (the ``/.fs``-backed tools
    continue to work; nothing about the journal surface leaks into the
    tool list).
    """
    if not config.enabled:
        return
    assert config.space_path is not None  # narrowed by JournalConfig.enabled
    root = Path(config.space_path)

    @mcp.tool(
        title="Journal histogram",
        description=(
            "Bucket `*.md` pages under the SB space directory by "
            "`YYYY-MM`, extracted from the daily-journal filename "
            "convention when present and from the file mtime otherwise. "
            "Restricted to pages whose relative path contains `prefix`."
        ),
    )
    async def journal_histogram(prefix: str = "") -> dict[str, int]:
        return await _journal_histogram(root, prefix)

    @mcp.tool(
        title="Tag summary",
        description=(
            "Count occurrences of every value under `tags:` in the YAML "
            "frontmatter of `*.md` pages under the SB space directory. "
            "Restricted to pages whose relative path contains `prefix`."
        ),
    )
    async def tag_summary(prefix: str = "") -> dict[str, int]:
        return await _tag_summary(root, prefix)

    @mcp.tool(
        title="Recent pages",
        description=(
            "Most-recently-modified `*.md` pages under the SB space "
            "directory, newest first. Each entry carries `name`, "
            "`mtime_iso`, and `size_bytes`. Restricted to pages whose "
            "relative path contains `prefix`."
        ),
    )
    async def recent_pages(
        limit: int = 10, prefix: str = ""
    ) -> list[dict[str, object]]:
        return [
            {
                "name": ref.name,
                "mtime_iso": ref.mtime_iso,
                "size_bytes": ref.size_bytes,
            }
            for ref in await _recent_pages(root, limit=limit, prefix=prefix)
        ]

    @mcp.tool(
        title="Pages touching topic",
        description=(
            "Search `*.md` pages under the SB space directory by both "
            "relative-path and body, case-insensitive substring. "
            "Returns one entry per match with `name`, the kind of "
            "match (`name`, `content`, or `both`), and a short "
            "Markdown-shaped snippet around the body match "
            "(or a body excerpt for name-only matches). Restricted "
            "to pages whose relative path contains `prefix`."
        ),
    )
    async def pages_touching_topic(
        query: str, prefix: str = ""
    ) -> list[dict[str, str]]:
        return await _pages_touching_topic(root, query, prefix)

    @mcp.tool(
        title="Search pages",
        description=(
            "Bounded substring content search over `*.md` pages under "
            "the SB space directory (T34). Same `name`/`content`/"
            "`both` match-kind and snippet shape as `pages_touching_topic` "
            "(T12), with a `limit` knob (default 20, hard cap 100) "
            "that bounds the result list. An agent that wants an "
            "unbounded scan of the space calls `pages_touching_topic` "
            "directly; `search_pages` exists for the common "
            "\"show me the top matches\" case where the response "
            "should be bounded by default. Restricted to pages whose "
            "relative path contains `prefix`. Empty/whitespace-only "
            "`query` is rejected before any FS walk."
        ),
    )
    async def search_pages(
        query: str,
        prefix: str = "",
        limit: int = _SEARCH_DEFAULT_LIMIT,
    ) -> list[dict[str, str]]:
        # Validate inputs at the boundary so the agent sees the
        # error without a wasted FS walk. ``_normalize_query`` is
        # idempotent — ``_pages_touching_topic`` also calls it, but
        # calling it here surfaces the error message before any
        # FS walk in the failure case.
        _normalize_query(query)
        _validate_prefix(prefix)
        return await _search_pages(root, query, prefix, limit)

    @mcp.tool(
        title="Find backlinks",
        description=(
            "Scan every `*.md` page under the SB space directory "
            "for wikilink references to `target`. Returns one "
            "entry per matching line with `file` (relative path "
            "to the linking page), `line` (1-indexed line "
            "number, matching editor conventions), and `text` "
            "(the stripped line text). `target` normalization: "
            "leading/trailing slashes and a trailing `.md` are "
            "stripped before matching, so `Projects/Foo`, "
            "`Projects/Foo.md`, and `/Projects/Foo/` all match "
            "the same canonical target. Aliases "
            "(`[[target|alias]]`) match the bare target. "
            "Self-links are included (the agent filters them "
            "client-side). Empty `target` raises `ToolError` "
            "upfront, before any FS walk. No matches returns "
            "`[]`, not a `ToolError` (the agent might be "
            "querying pre-emptively). The walk reuses the "
            "T11/T12 `_iter_md` machinery for `*.md` "
            "enumeration + hidden-directory skip; journal-"
            "gated like the rest of the discovery surface."
        ),
    )
    async def find_backlinks(
        target: str,
    ) -> list[dict[str, object]]:
        # Validate upfront so the agent sees the error before
        # any FS walk. An empty / whitespace-only target is
        # almost certainly a caller bug — surface it loudly
        # rather than returning the empty ``[]`` silently
        # (which would mask the typo and waste the agent's
        # time on a no-op rewrite). Mirrors the
        # ``text must not be empty`` / ``find must not be empty``
        # guards on the write tools.
        if not target or not target.strip():
            raise ToolError("target must not be empty")
        return await _find_backlinks(root, target)


__all__ = [
    "JournalConfig",
    "PageRef",
    "TaskEntry",
    "register_journal_tools",
    "resolve_journal_config",
]

"""Layer-1 tests for the T29 bullet-primitive surface.

The bullet primitives (``list_tasks`` and the parser helpers
:func:`mcp_silverbullet.journal._parse_tasks` /
:func:`mcp_silverbullet.journal._find_task_bullet`) live in two
places:

- The pure parser (``_parse_tasks``, ``_find_task_bullet``,
  ``_extract_first_wikilink``, ``_split_frontmatter_lines``) is
  in :mod:`mcp_silverbullet.journal` because the space-walk
  variant of ``list_tasks`` reuses the same parser.
- The per-page ``list_tasks`` MCP tool is in
  :mod:`mcp_silverbullet.server` because it closes over the
  ``SBClient``.

This file covers both halves:

- Direct unit tests for the parser (no MCP server, just
  function calls). These tests lock the wire-shape contract of
  :class:`mcp_silverbullet.journal.TaskEntry` so a future
  refactor that changes the field set / types / semantics
  surfaces as a test failure rather than as a silent
  downstream breakage.
- Layer-1 tests for the per-page ``list_tasks`` MCP tool, built
  on the same :func:`_build` / ``Client(server)`` pattern
  ``tests/test_tools_in_memory.py`` uses (so the same
  :func:`httpx.MockTransport` trick substitutes for a real
  SilverBullet).

The space-walk variant's per-page ``list_tasks`` test for
"omit page = space-walk" is in :mod:`tests.test_journal_gate`
(where the gate-on / gate-off shape already lives); this file
focuses on the parser's pure-function contract so a future
parser refactor can iterate against a tight test surface
without standing up an MCP server.
"""

from __future__ import annotations

from pathlib import Path

import httpx2 as httpx
import pytest
from mcp.client import Client

from mcp_silverbullet.journal import (
    JournalConfig,
    TaskEntry,
    _find_task_bullet,
    _parse_tasks,
)
from mcp_silverbullet.sb_client import SBClient
from mcp_silverbullet.server import build_mcp


TOKEN = "test-secret-do-not-use-in-prod"
RESOURCE_URL = "http://bridge.test/mcp"
SB_URL = "http://sb.test"


# --- direct parser unit tests ------------------------------------------


def test_parse_tasks_returns_empty_when_body_has_no_bullets() -> None:
    """A page with no ``- [ ]`` lines → ``[]``."""
    assert _parse_tasks("x.md", "# just a heading\n") == []


def test_parse_tasks_captures_the_three_state_characters() -> None:
    """``[ ]`` / ``[x]`` / ``[X]`` map to ``" "`` / ``"x"`` / ``"X"``.

    SB's editor renders a third state (``[X]`` for "cancelled")
    on top of todo (``[ ]``) and done (``[x]``). The parser
    keeps the literal checkbox character so the agent can
    tell them apart without re-parsing the bullet text.
    """
    body = "- [ ] todo\n- [x] done\n- [X] cancelled\n"
    entries = _parse_tasks("x.md", body)
    assert [e.state for e in entries] == [" ", "x", "X"]


def test_parse_tasks_ref_is_none_when_bullet_has_no_wikilink() -> None:
    """A bullet without a ``[[wikilink]]`` → ref = ``None``.

    Non-addressable bullets fall back to ``patch_page_lines``
    on the ``line`` field; the bridge doesn't try to add a
    synthetic wikilink (out of scope per the v1.2 standing
    preferences — destructive, the user didn't ask for it).
    """
    [entry] = _parse_tasks("x.md", "- [ ] plain bullet\n")
    assert entry.ref is None


def test_parse_tasks_text_strips_leading_whitespace() -> None:
    """``text`` is the bullet's content after the marker, leading whitespace trimmed.

    SB's editor renders the bullet text with leading spaces
    collapsed; the bridge matches so the agent reading a
    task list doesn't see ``" todo item"`` (with a leading
    space).
    """
    [entry] = _parse_tasks("x.md", "- [ ]     todo item\n")
    assert entry.text == "todo item"


def test_parse_tasks_skips_frontmatter_bullets() -> None:
    """YAML block-list ``- foo`` inside frontmatter is not a task.

    A page with ``tags:\\n  - foo`` frontmatter shouldn't
    surface ``foo`` as a task — it's a tag, not a checkbox.
    The frontmatter-detection rule (opening ``---`` fence,
    closing ``---`` fence, content between them) is shared
    with the tag parser; both treat the block as
    non-task-bearing.
    """
    body = (
        "---\n"
        "tags:\n"
        "  - foo\n"
        "  - bar\n"
        "---\n"
        "- [ ] real task\n"
    )
    entries = _parse_tasks("x.md", body)
    assert len(entries) == 1
    assert entries[0].ref is None
    assert entries[0].text == "real task"


def test_parse_tasks_skips_malformed_frontmatter() -> None:
    """An opening ``---`` with no closing fence → treat as body.

    A malformed page (e.g. ``---\\ntags: foo\\n# never closes``)
    shouldn't silently drop every task. The walker treats the
    whole body as body and the agent gets a "best effort" task
    list, including the YAML keys that happen to match the
    ``- [ ]`` pattern. Better than under-counting every task
    on a typo'd page.
    """
    body = "---\ntags: foo\n# never closes\n- [ ] real task\n"
    entries = _parse_tasks("x.md", body)
    # Two matches: the ``# never closes`` line isn't a bullet,
    # so the only checkbox match is ``- [ ] real task``.
    assert len(entries) == 1
    assert entries[0].text == "real task"


def test_parse_tasks_nested_bullets_match_at_any_indent() -> None:
    """Indented ``- [ ]`` lines count as tasks (sub-tasks)."""
    body = (
        "- [ ] outer [[O]]\n"
        "  - [ ] nested-1 [[N1]]\n"
        "    - [ ] deep [[N2]]\n"
    )
    entries = _parse_tasks("x.md", body)
    assert [e.ref for e in entries] == ["O", "N1", "N2"]
    assert [e.line for e in entries] == [1, 2, 3]


def test_parse_tasks_line_numbers_include_frontmatter() -> None:
    """Line numbers are 1-indexed against the *full* body.

    SB's editor counts lines including the frontmatter block,
    so a task on what looks like the second body line is on
    editor line ``N + 3`` (after a 1-line opening fence, ``N``
    frontmatter lines, and a 1-line closing fence).
    """
    body = (
        "---\n"          # line 1
        "tags: foo\n"      # line 2
        "---\n"            # line 3
        "\n"               # line 4 (blank)
        "- [ ] task\n"     # line 5
    )
    [entry] = _parse_tasks("x.md", body)
    assert entry.line == 5


def test_parse_tasks_does_not_match_regular_bullets() -> None:
    """``- bullet`` (no checkbox) is not a task."""
    entries = _parse_tasks("x.md", "- regular bullet\n- [ ] real task\n")
    assert len(entries) == 1
    assert entries[0].text == "real task"


def test_parse_tasks_does_not_match_checkbox_without_space_after_marker() -> None:
    """``-[ ]`` (no space before the marker) is not a task.

    SB's editor requires ``- [ ]`` (a space between the dash
    and the marker) to render a checkbox. The parser matches
    the same rule so an agent doesn't see ``-[ ]foo`` as a
    task (the editor wouldn't either).
    """
    entries = _parse_tasks("x.md", "-[ ]not a task\n- [ ] real task\n")
    assert len(entries) == 1
    assert entries[0].text == "real task"


def test_find_task_bullet_returns_none_for_empty_ref() -> None:
    """An empty ref is treated as "no match" (caller bug)."""
    body = "- [ ] task with [[Pages/Hobbies]] ref\n"
    assert _find_task_bullet(body, "") is None


def test_find_task_bullet_returns_none_when_no_bullet_matches() -> None:
    """No bullet with the ref → ``None``."""
    body = "- [ ] task with [[Other]] ref\n"
    assert _find_task_bullet(body, "Pages/Hobbies") is None


def test_find_task_bullet_returns_byte_offsets_for_splice() -> None:
    """The returned byte offsets let the caller splice a new body.

    T30's :func:`check_task` flips the checkbox character
    without rewriting the whole line; the byte offsets let
    the caller ``body[:start] + modified_line + body[end:]``
    in O(1) instead of re-walking lines.
    """
    body = "header\n- [ ] task with [[Ref]] ref\ntrailer\n"
    match = _find_task_bullet(body, "Ref")
    assert match is not None
    _, _, text, start, end = match
    spliced = body[:start] + "REPLACED" + body[end:]
    # The replacement replaced the entire bullet line; the
    # ``\n`` that was on the *outside* of the line (between
    # ``header\n`` and ``trailer\n``) survives — the splice
    # is byte-exact.
    assert spliced == "header\nREPLACED\ntrailer\n"


def test_find_task_bullet_returns_first_match_when_multiple() -> None:
    """Multi-match → first match returned (caller surfaces the multi-match error)."""
    body = (
        "- [ ] first [[Same]]\n"
        "- [ ] second [[Same]]\n"
    )
    match = _find_task_bullet(body, "Same")
    assert match is not None
    editor_line, _, text, _, _ = match
    assert editor_line == 1
    assert text == "first [[Same]]"


def test_find_task_bullet_case_sensitive_match() -> None:
    """``Pages/Hobbies`` ≠ ``pages/hobbies`` — file-system case-sensitive.

    The bridge matches SB's page lookup exactly so an agent
    that passes the wrong-case ref sees a "no match" rather
    than silently toggling a different task.
    """
    body = "- [ ] task with [[Pages/Hobbies]] ref\n"
    assert _find_task_bullet(body, "Pages/Hobbies") is not None
    assert _find_task_bullet(body, "pages/hobbies") is None


def test_find_task_bullet_handles_trailing_newline_correctly() -> None:
    """Byte offsets work correctly for bodies that end with ``\\n``.

    SB stores text with a trailing newline the way editors
    do; the byte-offset math has to match. Quick check:
    splicing the modification back yields the expected body
    (the trailing newline survives the splice because it's
    *outside* the bullet's byte range).
    """
    body = "header\n- [ ] task with [[Ref]] ref\n"
    match_with = _find_task_bullet(body, "Ref")
    assert match_with is not None
    _, _, _, start, end = match_with
    spliced = body[:start] + "REPLACED" + body[end:]
    assert spliced == "header\nREPLACED\n"

    without_nl = "header\n- [ ] task with [[Ref]] ref"
    match_without = _find_task_bullet(without_nl, "Ref")
    assert match_without is not None
    _, _, _, start, end = match_without
    spliced = without_nl[:start] + "REPLACED" + without_nl[end:]
    # No trailing newline in the input → no trailing newline
    # in the splice. The byte range covers exactly the bullet
    # line (no implicit newline attached).
    assert spliced == "header\nREPLACED"


def test_find_task_bullet_returns_none_for_empty_body() -> None:
    """Empty body → ``None`` (no bullet can match)."""
    assert _find_task_bullet("", "anything") is None


# --- TaskEntry dataclass shape -----------------------------------------


def test_task_entry_fields_match_the_wire_shape() -> None:
    """``TaskEntry`` carries the five fields the tool wire shape advertises.

    ``name`` (page), ``line`` (1-indexed editor line), ``state``
    (``" "`` / ``"x"`` / ``"X"``), ``ref`` (``str | None``),
    ``text`` (bullet content after marker). Adding a field is
    a breaking change (the JSON dump shape grows); removing a
    field is also a breaking change (the tool returns less).
    """
    entry = TaskEntry(
        name="x.md",
        line=5,
        state=" ",
        ref="Ref",
        text="task with [[Ref]] ref",
    )
    # ``dataclasses.asdict`` round-trip the field set so a
    # future edit that adds/removes/renames a field surfaces
    # loudly here.
    from dataclasses import asdict

    assert asdict(entry) == {
        "name": "x.md",
        "line": 5,
        "state": " ",
        "ref": "Ref",
        "text": "task with [[Ref]] ref",
    }


# --- space-walk form (Layer-1 MCP) -------------------------------------
#
# These tests stand up the full MCP server (with the journal
# gate on) and exercise ``list_tasks(page=None)`` against a
# ``tmp_path`` populated with synthetic ``*.md`` files. The
# space-walk form is opt-in (gated by the journal tools env
# vars); the per-page form's MCP-level tests live in
# ``tests/test_tools_in_memory.py`` under the
# ``# --- list_tasks (T29) ---`` section header.


def _build_with_journal(space_path: Path):
    """Build an MCP server whose journal gate points at ``space_path``.

    Mirrors the helper in :mod:`tests.test_journal_read` but
    inlines it here so the task-specific tests don't depend on
    that module's helper naming. (``_build`` in
    ``test_journal_read`` returns ``None`` if the SB transport
    raises; the task tests don't need the SB transport at all
    because the space-walk form bypasses ``sb_client``.)
    """
    transport = httpx.MockTransport(lambda req: httpx.Response(200))
    sb = SBClient.__new__(SBClient)
    sb._client = httpx.AsyncClient(
        base_url=SB_URL,
        headers={"Authorization": f"Bearer {TOKEN}"},
        transport=transport,
    )
    return build_mcp(
        sb,
        token=TOKEN,
        resource_url=RESOURCE_URL,
        journal=JournalConfig(enabled=True, space_path=str(space_path)),
    )


def _write(tmp: Path, name: str, body: str) -> None:
    """Write ``body`` to ``tmp/name`` (creating parent dirs as needed)."""
    path = tmp / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


@pytest.mark.asyncio
async def test_list_tasks_space_walk_returns_tasks_from_every_page(
    tmp_path: Path,
) -> None:
    """Omitting ``page`` walks every ``*.md`` page and returns one entry per bullet.

    Tasks from multiple pages are flattened into a single
    sorted-by-``(name, line)`` list so the agent reading a
    space-wide task summary doesn't have to re-sort client-side.
    """
    _write(tmp_path, "Areas/Kanban.md", "- [ ] kanban task [[K]]\n")
    _write(tmp_path, "Daily/2026-01-05.md", "- [x] done journal task\n")
    _write(tmp_path, "no-tasks.md", "no bullets here\n")
    server = _build_with_journal(tmp_path)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("list_tasks", {})
    sc = result.structured_content
    assert sc["result"] == [
        {"name": "Areas/Kanban.md", "ref": "K", "line": 1, "state": " ", "text": "kanban task [[K]]"},
        {"name": "Daily/2026-01-05.md", "ref": None, "line": 1, "state": "x", "text": "done journal task"},
    ]


@pytest.mark.asyncio
async def test_list_tasks_space_walk_skips_hidden_directories(
    tmp_path: Path,
) -> None:
    """``.git/HEAD.md`` and friends don't leak into the space-wide list."""
    _write(tmp_path, "Daily.md", "- [ ] real task\n")
    _write(tmp_path, ".git/HEAD.md", "- [ ] fake task\n")
    _write(tmp_path, ".cache/index.md", "- [ ] fake task\n")
    server = _build_with_journal(tmp_path)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("list_tasks", {})
    sc = result.structured_content
    assert sc["result"] == [
        {"name": "Daily.md", "ref": None, "line": 1, "state": " ", "text": "real task"},
    ]


@pytest.mark.asyncio
async def test_list_tasks_space_walk_prefix_filters_to_subtree(
    tmp_path: Path,
) -> None:
    """A ``prefix`` of ``"Daily"`` restricts the walk to ``Daily/*`` pages."""
    _write(tmp_path, "Daily/2026-01-05.md", "- [ ] journal task [[J]]\n")
    _write(tmp_path, "Areas/Kanban.md", "- [ ] kanban task [[K]]\n")
    server = _build_with_journal(tmp_path)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("list_tasks", {"prefix": "Daily"})
    sc = result.structured_content
    assert sc["result"] == [
        {"name": "Daily/2026-01-05.md", "ref": "J", "line": 1, "state": " ", "text": "journal task [[J]]"},
    ]


@pytest.mark.asyncio
async def test_list_tasks_space_walk_rejects_dot_dot_prefix(
    tmp_path: Path,
) -> None:
    """The same path-traversal guard the journal tools use (``..`` rejected)."""
    server = _build_with_journal(tmp_path)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "list_tasks", {"prefix": "../etc"}
        )
    assert result.is_error is True
    text = "".join(
        block.text for block in result.content
        if getattr(block, "type", None) == "text"
    )
    assert ".." in text


@pytest.mark.asyncio
async def test_list_tasks_space_walk_returns_empty_when_space_empty(
    tmp_path: Path,
) -> None:
    """Empty space → empty list (no error)."""
    server = _build_with_journal(tmp_path)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("list_tasks", {})
    assert result.is_error is False
    assert result.structured_content == {"result": []}


@pytest.mark.asyncio
async def test_list_tasks_space_walk_skips_unreadable_files(
    tmp_path: Path,
) -> None:
    """Files that fail to decode (encoding error) are skipped, not fatal.

    A mixed-encoding space (one UTF-16 file alongside UTF-8
    pages) shouldn't fail the whole walk; the walker logs
    the skip and continues. Same shape as the
    ``_recent_pages`` tolerance for transient FS races.
    """
    _write(tmp_path, "good.md", "- [ ] good task\n")
    # A file with bytes that aren't valid UTF-8 — write_text
    # with ``encoding="utf-8"`` rejects this, so we use
    # ``write_bytes`` and let the walker's ``read_text`` raise.
    bad = tmp_path / "bad.md"
    bad.write_bytes(b"- [ ] \xff\xfe bad task\n")
    server = _build_with_journal(tmp_path)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("list_tasks", {})
    sc = result.structured_content
    assert sc["result"] == [
        {"name": "good.md", "ref": None, "line": 1, "state": " ", "text": "good task"},
    ]

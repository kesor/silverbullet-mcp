"""Layer-1 tests for ``find_backlinks`` (T35).

The T10 skeleton-error test for ``find_backlinks`` was inverted in
``tests/test_journal_gate.py``: now the tool boots cleanly and
returns ``[]`` against an empty ``tmp_path``. The real behaviors —
single reference, multiple references, aliased references, target
normalization (``Projects/Foo`` / ``Projects/Foo.md`` /
``/Projects/Foo/``), self-links, no-match empty-result, empty-target
upfront ``ToolError``, hidden-directory skip — live here.

The wikilink regex is module-private
(``_BACKLINK_WIKILINK_RE`` in ``mcp_silverbullet/journal.py``); the
helper that consumes it (``_find_backlinks``) and the normalization
helper (``_normalize_link_target``) are also module-private. These
tests go through the public ``find_backlinks`` MCP tool to lock the
wire shape (`{file, line, text}` per the
``lidiaev/me-db``-style contract documented in the T35 ticket).

Each test constructs an in-memory MCP server whose
``JournalConfig.enabled`` points at a ``tmp_path`` populated with
synthetic ``*.md`` files. No live FS walking needed beyond the
``tmp_path`` the fixture creates — ``_find_backlinks`` walks the
space root directly (it's a journal-surface tool, not an ``/.fs``
tool).
"""

from __future__ import annotations

from pathlib import Path

import httpx2 as httpx
import pytest
from mcp.client import Client

from mcp_silverbullet.journal import JournalConfig
from mcp_silverbullet.sb_client import SBClient
from mcp_silverbullet.server import build_mcp


TOKEN = "test-secret-do-not-use-in-prod"
RESOURCE_URL = "http://bridge.test/mcp"


def _build(space_path: Path):
    transport = httpx.MockTransport(lambda req: httpx.Response(200))
    sb = SBClient.__new__(SBClient)
    sb._client = httpx.AsyncClient(
        base_url="http://sb.test",
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
    path = tmp / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def _entries(result) -> list[dict[str, object]]:
    return list(result.structured_content["result"])


# --- input validation --------------------------------------------------


@pytest.mark.asyncio
async def test_find_backlinks_rejects_empty_target(
    tmp_path: Path,
) -> None:
    """Empty ``target`` → ``ToolError`` upfront, before any FS walk.

    Mirrors the ``text must not be empty`` /
    ``find must not be empty`` guards on the write tools. An empty
    target is almost certainly a caller bug; surfacing it loudly
    saves the agent a wasted FS walk.
    """
    server = _build(tmp_path)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("find_backlinks", {"target": ""})
    assert result.is_error is True
    assert "target must not be empty" in result.content[0].text


@pytest.mark.asyncio
async def test_find_backlinks_rejects_whitespace_only_target(
    tmp_path: Path,
) -> None:
    """``"   "`` → ``ToolError`` (whitespace-only is empty after normalization)."""
    server = _build(tmp_path)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "find_backlinks", {"target": "   \n  "}
        )
    assert result.is_error is True
    assert "target must not be empty" in result.content[0].text


# --- happy path -------------------------------------------------------


@pytest.mark.asyncio
async def test_find_backlinks_single_reference_returns_one_entry(
    tmp_path: Path,
) -> None:
    """A page with one ``[[target]]`` reference → one entry.

    Locks the basic shape: ``{file, line, text}`` per the
    ``lidiaev/me-db``-style contract. ``file`` is the relative
    path; ``line`` is 1-indexed; ``text`` is the stripped line.
    """
    _write(tmp_path, "linker.md", "Intro paragraph\nSee [[Projects/Foo]] for details\n")
    server = _build(tmp_path)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "find_backlinks", {"target": "Projects/Foo"}
        )

    assert result.is_error is False
    entries = _entries(result)
    assert len(entries) == 1
    entry = entries[0]
    assert entry["file"] == "linker.md"
    assert entry["line"] == 2
    assert entry["text"] == "See [[Projects/Foo]] for details"


@pytest.mark.asyncio
async def test_find_backlinks_multiple_references_on_different_lines(
    tmp_path: Path,
) -> None:
    """A page with three references on three lines → three entries.

    Locked to the wire contract: each match is one entry (one per
    line, not one per wikilink). A page with three
    ``[[Projects/Foo]]`` references on three different lines
    returns three entries with three different ``line`` values
    and the corresponding ``text``.
    """
    _write(
        tmp_path,
        "linker.md",
        "First [[Projects/Foo]] here\n"
        "Body paragraph\n"
        "Another [[Projects/Foo]] there\n"
        "Tail [[Projects/Foo]] finally\n",
    )
    server = _build(tmp_path)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "find_backlinks", {"target": "Projects/Foo"}
        )

    assert result.is_error is False
    entries = _entries(result)
    assert len(entries) == 3
    assert [e["line"] for e in entries] == [1, 3, 4]
    assert entries[0]["text"] == "First [[Projects/Foo]] here"
    assert entries[1]["text"] == "Another [[Projects/Foo]] there"
    assert entries[2]["text"] == "Tail [[Projects/Foo]] finally"


@pytest.mark.asyncio
async def test_find_backlinks_multiple_references_on_one_line_collapse_to_one_entry(
    tmp_path: Path,
) -> None:
    """``[[Foo]] [[Foo]] [[Foo]]`` on one line → one entry (per-line granularity).

    Index pages sometimes cram multiple wikilinks onto a single
    line. The wire contract is *one entry per matching line*,
    not one entry per matching wikilink — the agent that wants
    per-match granularity calls ``rg`` themselves. This test
    pins that behavior so a future refactor that switched to
    per-wikilink entries (which would silently change the
    return shape) fails loudly.
    """
    _write(
        tmp_path,
        "index.md",
        "Related: [[Projects/Foo]] [[Projects/Foo]] [[Projects/Foo]]\n",
    )
    server = _build(tmp_path)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "find_backlinks", {"target": "Projects/Foo"}
        )

    assert result.is_error is False
    entries = _entries(result)
    assert len(entries) == 1
    assert entries[0]["line"] == 1
    # The text is the full line, stripped (not just the first
    # wikilink). The agent reading this sees the surrounding
    # context.
    assert entries[0]["text"] == (
        "Related: [[Projects/Foo]] [[Projects/Foo]] [[Projects/Foo]]"
    )


@pytest.mark.asyncio
async def test_find_backlinks_aliased_reference_matches_bare_target(
    tmp_path: Path,
) -> None:
    """``[[target|alias]]`` matches the bare ``target``.

    Aliases are the *display* text in SB, not a different page.
    ``[[Projects/Foo|the foo project]]`` should match the
    query ``Projects/Foo`` exactly the same way
    ``[[Projects/Foo]]`` does. Locks the alias-stripping
    invariant from the T35 ticket.
    """
    _write(
        tmp_path,
        "linker.md",
        "See [[Projects/Foo|the foo project]] for context\n",
    )
    server = _build(tmp_path)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "find_backlinks", {"target": "Projects/Foo"}
        )

    assert result.is_error is False
    entries = _entries(result)
    assert len(entries) == 1
    # The text is the stripped line (including the alias);
    # the agent sees the alias in context.
    assert entries[0]["text"] == (
        "See [[Projects/Foo|the foo project]] for context"
    )


@pytest.mark.asyncio
async def test_find_backlinks_aliased_target_does_not_match_bare(
    tmp_path: Path,
) -> None:
    """``[[Projects/Foo]]`` does NOT match the query ``Foo``.

    SB's page lookup is case-sensitive and full-segment; the
    alias is *display* text and the target is the page *name*.
    ``[[Foo|alias]]`` resolves to ``Foo``, not to ``alias`` —
    and ``[[Projects/Foo]]`` resolves to ``Projects/Foo``, not
    to ``Foo``. The normalized comparator is an exact string
    equality after ``_normalize_link_target``; an aliased
    target string never matches a bare-prefix query. This test
    pins the *not-match* direction of the alias invariant
    (paired with the previous test's *match* direction).
    """
    _write(tmp_path, "linker.md", "See [[Projects/Foo]] here\n")
    server = _build(tmp_path)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "find_backlinks", {"target": "Foo"}
        )

    assert result.is_error is False
    assert _entries(result) == []


# --- target normalization ---------------------------------------------


@pytest.mark.asyncio
async def test_find_backlinks_query_with_trailing_md_matches(
    tmp_path: Path,
) -> None:
    """``target = "Projects/Foo.md"`` matches ``[[Projects/Foo]]``.

    SB stores pages as ``*.md`` on disk but wikilink resolution
    happens against the page *name*, not the file extension.
    Querying with ``.md`` should match the same pages as
    querying without. Locks the normalization rule from
    ``_normalize_link_target``.
    """
    _write(tmp_path, "linker.md", "See [[Projects/Foo]] for context\n")
    server = _build(tmp_path)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "find_backlinks", {"target": "Projects/Foo.md"}
        )

    assert result.is_error is False
    assert len(_entries(result)) == 1


@pytest.mark.asyncio
async def test_find_backlinks_query_with_leading_and_trailing_slashes_matches(
    tmp_path: Path,
) -> None:
    """``target = "/Projects/Foo/"`` matches ``[[Projects/Foo]]``.

    SB accepts a leading slash as the space-root anchor; the
    canonical form has no anchor. Trailing slashes are
    accepted but stripped before matching.
    """
    _write(tmp_path, "linker.md", "See [[Projects/Foo]] for context\n")
    server = _build(tmp_path)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "find_backlinks", {"target": "/Projects/Foo/"}
        )

    assert result.is_error is False
    assert len(_entries(result)) == 1


@pytest.mark.asyncio
async def test_find_backlinks_target_with_md_extension_inside_link_matches(
    tmp_path: Path,
) -> None:
    """A wikilink whose target ends in ``.md`` (``[[Projects/Foo.md]]``)
    matches a query for the bare ``Projects/Foo``.

    The normalization runs on *both* sides of the comparator:
    the query target is normalized, and each wikilink target
    is normalized. So ``[[Projects/Foo.md]]`` normalizes to
    ``Projects/Foo``, matching the query ``Projects/Foo``.
    """
    _write(tmp_path, "linker.md", "See [[Projects/Foo.md]] for context\n")
    server = _build(tmp_path)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "find_backlinks", {"target": "Projects/Foo"}
        )

    assert result.is_error is False
    assert len(_entries(result)) == 1


@pytest.mark.asyncio
async def test_find_backlinks_is_case_sensitive(
    tmp_path: Path,
) -> None:
    """``Projects/Foo`` does NOT match ``projects/foo`` (SB page lookup is case-sensitive).

    The v1 T6 / v1.2 T30 carry-forwards document SB's page
    lookup as case-sensitive. The normalization helper does not
    case-fold; a query for ``projects/foo`` looks for that
    exact normalized string and the wikilink
    ``[[Projects/Foo]]`` normalizes to ``Projects/Foo`` (a
    different string). Locks the case-sensitivity invariant.
    """
    _write(tmp_path, "linker.md", "See [[Projects/Foo]] for context\n")
    server = _build(tmp_path)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "find_backlinks", {"target": "projects/foo"}
        )

    assert result.is_error is False
    assert _entries(result) == []


# --- self-links and missing-target ------------------------------------


@pytest.mark.asyncio
async def test_find_backlinks_self_link_is_returned(
    tmp_path: Path,
) -> None:
    """``Projects/Foo`` containing ``[[Projects/Foo]]`` returns the self-link.

    Self-links are valid backlinks — the agent that wants to
    filter them does so client-side (filter ``entry["file"]
    == target``). The bridge doesn't presume; the T35 ticket
    explicitly documents self-links as a *returned* case.
    """
    _write(tmp_path, "Projects/Foo.md", "Intro\nSelf-ref [[Projects/Foo]] here\n")
    server = _build(tmp_path)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "find_backlinks", {"target": "Projects/Foo"}
        )

    assert result.is_error is False
    entries = _entries(result)
    assert len(entries) == 1
    assert entries[0]["file"] == "Projects/Foo.md"
    assert entries[0]["line"] == 2


@pytest.mark.asyncio
async def test_find_backlinks_no_matches_returns_empty_list(
    tmp_path: Path,
) -> None:
    """No matches → ``[]``, not a ``ToolError``.

    The agent might be querying pre-emptively ("am I about to
    break anything if I rename this page?") and a missing
    target is a legitimate answer. A ``ToolError`` here would
    force the agent to special-case the empty-list return.
    Locks the T35 ticket's empty-result contract.
    """
    _write(tmp_path, "linker.md", "Body with no wikilinks\n")
    server = _build(tmp_path)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "find_backlinks", {"target": "Projects/Missing"}
        )

    assert result.is_error is False
    assert _entries(result) == []


@pytest.mark.asyncio
async def test_find_backlinks_empty_space_returns_empty_list(
    tmp_path: Path,
) -> None:
    """Empty ``tmp_path`` (no ``*.md`` files) → ``[]``."""
    server = _build(tmp_path)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "find_backlinks", {"target": "anything"}
        )

    assert result.is_error is False
    assert _entries(result) == []


# --- walker behavior --------------------------------------------------


@pytest.mark.asyncio
async def test_find_backlinks_walks_multiple_pages(
    tmp_path: Path,
) -> None:
    """Three linking pages in three directories → three entries (one per page)."""
    _write(
        tmp_path,
        "Daily/2026-01-05.md",
        "Pre-market notes\nSee [[Projects/Foo]]\n",
    )
    _write(
        tmp_path,
        "Projects/Bar.md",
        "Body\nReference: [[Projects/Foo]]\n",
    )
    _write(
        tmp_path,
        "index.md",
        "Top-level\n[[Projects/Foo]] somewhere\n",
    )
    server = _build(tmp_path)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "find_backlinks", {"target": "Projects/Foo"}
        )

    assert result.is_error is False
    entries = _entries(result)
    assert len(entries) == 3
    files = sorted(e["file"] for e in entries)
    assert files == [
        "Daily/2026-01-05.md",
        "Projects/Bar.md",
        "index.md",
    ]


@pytest.mark.asyncio
async def test_find_backlinks_skips_hidden_directories(
    tmp_path: Path,
) -> None:
    """Pages under ``.cache/`` / ``.git/`` are skipped (no operator-visible links).

    Mirrors :func:`_iter_md`'s hidden-directory skip. A wikilink
    in ``.cache/notes.md`` should not surface — operators don't
    author links into hidden directories, and a stray
    ``.git/index.md`` would be a bug to surface, not a feature.
    Locks the same hidden-dir skip the rest of the journal
    surface uses.
    """
    _write(tmp_path, "visible.md", "See [[Projects/Foo]]\n")
    _write(tmp_path, ".cache/notes.md", "Should be skipped [[Projects/Foo]]\n")
    _write(tmp_path, ".git/index.md", "Should be skipped [[Projects/Foo]]\n")
    server = _build(tmp_path)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "find_backlinks", {"target": "Projects/Foo"}
        )

    assert result.is_error is False
    entries = _entries(result)
    assert len(entries) == 1
    assert entries[0]["file"] == "visible.md"


@pytest.mark.asyncio
async def test_find_backlinks_unreadable_page_is_skipped_silently(
    tmp_path: Path,
) -> None:
    """A page with non-UTF-8 bytes doesn't crash the scan (best-effort walker).

    Locks the ``except (OSError, UnicodeDecodeError): continue``
    stance from :func:`_find_backlinks`: a single bad page
    shouldn't abort the whole backlink scan. Matches the v1
    T11 / T12 walker's stance on the same error class.
    """
    _write(tmp_path, "visible.md", "See [[Projects/Foo]]\n")
    bad = tmp_path / "binary.md"
    bad.write_bytes(b"\xff\xfe not utf-8 [[Projects/Foo]] here")
    server = _build(tmp_path)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "find_backlinks", {"target": "Projects/Foo"}
        )

    assert result.is_error is False
    entries = _entries(result)
    # Only the visible page's match; the binary page is
    # skipped silently.
    assert len(entries) == 1
    assert entries[0]["file"] == "visible.md"


# --- line numbering ---------------------------------------------------


@pytest.mark.asyncio
async def test_find_backlinks_line_numbers_are_one_indexed(
    tmp_path: Path,
) -> None:
    """``line`` is 1-indexed, matching editor conventions.

    The first line of a page is line 1, not line 0. Matches
    the ``pages_touching_topic`` snippet shape (which counts
    from line 1) and the ``patch_page_lines`` argument
    contract (``start_line >= 1``). A regression that
    zero-indexed would silently misalign every caller that
    threads the line number back into ``patch_page_lines``.
    """
    _write(
        tmp_path,
        "linker.md",
        "Line 1\nLine 2\nLine 3 [[Projects/Foo]]\n",
    )
    server = _build(tmp_path)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "find_backlinks", {"target": "Projects/Foo"}
        )

    assert result.is_error is False
    entries = _entries(result)
    assert entries[0]["line"] == 3
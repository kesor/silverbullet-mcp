"""Layer-1 tests for the three T11 read tools.

The T10 skeleton-error tests in ``tests/test_journal_gate.py`` were
inverted: the four tools no longer raise ``ToolError`` on every
call. Three of them — ``journal_histogram``, ``tag_summary``,
``recent_pages`` — now read the SB space directory directly and
return their real shapes. ``pages_touching_topic`` (T12) is still a
skeleton and lives in ``test_journal_gate.py``.

These tests construct an in-memory MCP server whose
``MCP_SILVERBULLET_SPACE_PATH`` points at a ``tmp_path`` populated
with synthetic ``*.md`` files (one with daily-journal filename +
frontmatter tags, one with a non-dated name, one with no frontmatter,
one in a subdirectory, one in a hidden directory to confirm it's
skipped). Each assertion is on the wire payload (``is_error``,
``structured_content``, error text) so a future SDK rename of the
registry attribute doesn't break the test.
"""

from __future__ import annotations

import datetime as _dt
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
    """Build an MCP server whose ``JournalConfig.enabled`` points at ``space_path``."""
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


def _text(result) -> str:
    """Concatenate the text content of a tool call result."""
    return "".join(
        block.text for block in result.content if getattr(block, "type", None) == "text"
    )


def _write(tmp: Path, name: str, body: str) -> None:
    """Write ``body`` to ``tmp/name`` (creating parent dirs as needed)."""
    path = tmp / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


# --- journal_histogram -------------------------------------------------


@pytest.mark.asyncio
async def test_journal_histogram_empty_space_returns_empty(tmp_path: Path) -> None:
    server = _build(tmp_path)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("journal_histogram", {})
    assert result.is_error is False
    # The SDK turns ``dict[str, int]`` returns into a RootModel — the
    # payload is the dict itself, not wrapped in ``{"result": ...}``.
    assert result.structured_content == {}


@pytest.mark.asyncio
async def test_journal_histogram_buckets_by_filename_date(tmp_path: Path) -> None:
    """Daily-journal filenames win over mtime; ``YYYY-MM-DD.md`` → ``YYYY-MM`` key."""
    _write(tmp_path, "Daily/2026-01-05.md", "# jan")
    _write(tmp_path, "Daily/2026-01-09.md", "# jan")
    _write(tmp_path, "Daily/2026-02-01.md", "# feb")
    server = _build(tmp_path)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("journal_histogram", {})
    assert result.structured_content == {
        "2026-01": 2, "2026-02": 1
    }


@pytest.mark.asyncio
async def test_journal_histogram_falls_back_to_mtime_for_undated_files(
    tmp_path: Path,
) -> None:
    """Files without a ``YYYY-MM-DD`` prefix bucket by mtime (UTC)."""
    # Pin the mtime by setting it explicitly; ``os.utime`` uses ns.
    nondated = tmp_path / "ideas/random-note.md"
    nondated.parent.mkdir(parents=True, exist_ok=True)
    nondated.write_text("# note", encoding="utf-8")
    ts = _dt.datetime(2025, 7, 14, 12, 0, 0, tzinfo=_dt.timezone.utc)
    ns = int(ts.timestamp() * 1_000_000_000)
    # ``Path.touch`` doesn't take an ns timestamp; use ``os.utime``.
    import os

    os.utime(nondated, ns=(ns, ns))
    server = _build(tmp_path)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("journal_histogram", {})
    assert result.structured_content == {"2025-07": 1}


@pytest.mark.asyncio
async def test_journal_histogram_prefix_filters_to_subtree(tmp_path: Path) -> None:
    """A ``prefix`` of ``"Daily"`` restricts to files whose relative path contains it."""
    _write(tmp_path, "Daily/2026-01-05.md", "# jan")
    _write(tmp_path, "Daily/2026-02-01.md", "# feb")
    _write(tmp_path, "Areas/Daily Notes.md", "# other daily")
    _write(tmp_path, "Library/static.md", "# static")
    server = _build(tmp_path)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "journal_histogram", {"prefix": "Daily"}
        )
    # ``Areas/Daily Notes.md`` matches the substring ``"Daily"``
    # even though it's not under the Daily directory — the prefix
    # is a substring against the relative path, not a directory
    # match. That file has no date in its name, so it buckets to
    # its mtime (current month); the dated Daily/* files bucket to
    # ``2026-01`` / ``2026-02``. The exact ``2026-08`` key is
    # ``datetime.now(tz=UTC)``-dependent, so assert the keys exist
    # and that the dated counts are right.
    sc = result.structured_content
    assert sc["2026-01"] == 1
    assert sc["2026-02"] == 1
    assert sum(sc.values()) == 3


@pytest.mark.asyncio
async def test_journal_histogram_skips_hidden_directories(tmp_path: Path) -> None:
    """``.git``, ``.cache``, ``.ssh`` are not enumerated even when they contain ``*.md``."""
    _write(tmp_path, "Daily/2026-01-05.md", "# visible")
    _write(tmp_path, ".git/HEAD.md", "# git noise")
    _write(tmp_path, ".cache/index.md", "# cache noise")
    _write(tmp_path, ".ssh/config.md", "# ssh noise")
    server = _build(tmp_path)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("journal_histogram", {})
    assert result.structured_content == {"2026-01": 1}


@pytest.mark.asyncio
async def test_journal_histogram_rejects_dot_dot_prefix(tmp_path: Path) -> None:
    server = _build(tmp_path)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "journal_histogram", {"prefix": "../etc"}
        )
    assert result.is_error is True
    assert ".." in _text(result)


@pytest.mark.asyncio
async def test_journal_histogram_rejects_absolute_prefix(tmp_path: Path) -> None:
    server = _build(tmp_path)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "journal_histogram", {"prefix": "/etc"}
        )
    assert result.is_error is True
    assert "/" in _text(result)


# --- tag_summary -------------------------------------------------------


@pytest.mark.asyncio
async def test_tag_summary_empty_space_returns_empty(tmp_path: Path) -> None:
    server = _build(tmp_path)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("tag_summary", {})
    assert result.structured_content == {}


@pytest.mark.asyncio
async def test_tag_summary_counts_scalar_and_list_tags(tmp_path: Path) -> None:
    """Scalar and block-list tag shapes are both counted; case preserved."""
    _write(
        tmp_path,
        "a.md",
        "---\ntags: book-review\n---\n\nbody\n",
    )
    _write(
        tmp_path,
        "b.md",
        "---\ntags:\n  - daily-journal\n  - work\n---\n\nbody\n",
    )
    _write(
        tmp_path,
        "c.md",
        "---\ntags:\n  - work\n  - meta\n---\n\nbody\n",
    )
    _write(tmp_path, "d.md", "no frontmatter here\n")
    _write(
        tmp_path,
        "e.md",
        "---\ntags: 'meta'\n---\n\nbody\n",  # single-quoted scalar
    )
    server = _build(tmp_path)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("tag_summary", {})
    # Sorted by count desc, ties broken by tag asc.
    assert result.structured_content == {
        "work": 2, "meta": 2, "book-review": 1, "daily-journal": 1
    }


@pytest.mark.asyncio
async def test_tag_summary_preserves_case(tmp_path: Path) -> None:
    """``daily`` and ``Daily`` are different keys (no case folding)."""
    _write(tmp_path, "a.md", "---\ntags: daily\n---\n\nbody\n")
    _write(tmp_path, "b.md", "---\ntags: Daily\n---\n\nbody\n")
    server = _build(tmp_path)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("tag_summary", {})
    assert result.structured_content == {"daily": 1, "Daily": 1}


@pytest.mark.asyncio
async def test_tag_summary_strips_surrounding_quotes(tmp_path: Path) -> None:
    """Tagged with quotes (``"foo"`` / ``'foo'``) collapses to ``foo``."""
    _write(tmp_path, "a.md", '---\ntags: "meta"\n---\n\nbody\n')
    _write(tmp_path, "b.md", "---\ntags: 'meta'\n---\n\nbody\n")
    _write(tmp_path, "c.md", "---\ntags: meta\n---\n\nbody\n")
    server = _build(tmp_path)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("tag_summary", {})
    assert result.structured_content == {"meta": 3}


@pytest.mark.asyncio
async def test_tag_summary_handles_malformed_frontmatter(tmp_path: Path) -> None:
    """A file that looks like frontmatter but never closes → empty tag list."""
    _write(tmp_path, "broken.md", "---\ntags: foo\n# never closes\n")
    _write(tmp_path, "good.md", "---\ntags: real\n---\n\nbody\n")
    server = _build(tmp_path)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("tag_summary", {})
    # The broken file contributes nothing; the good file counts ``real``.
    assert result.structured_content == {"real": 1}


@pytest.mark.asyncio
async def test_tag_summary_prefix_filters_to_subtree(tmp_path: Path) -> None:
    _write(tmp_path, "Areas/Contacts/a.md", "---\ntags: contact\n---\n\n")
    _write(tmp_path, "Areas/Books/b.md", "---\ntags: book-review\n---\n\n")
    server = _build(tmp_path)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("tag_summary", {"prefix": "Contacts"})
    assert result.structured_content == {"contact": 1}


# --- recent_pages ------------------------------------------------------


@pytest.mark.asyncio
async def test_recent_pages_empty_space_returns_empty(tmp_path: Path) -> None:
    server = _build(tmp_path)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("recent_pages", {})
    assert result.structured_content == {"result": []}


@pytest.mark.asyncio
async def test_recent_pages_returns_newest_first(tmp_path: Path) -> None:
    """``recent_pages`` orders by mtime desc; default ``limit=10``."""
    import os

    base = _dt.datetime(2026, 1, 1, 12, 0, 0, tzinfo=_dt.timezone.utc)
    for offset, name in enumerate(["a.md", "b.md", "c.md"]):
        path = tmp_path / name
        path.write_text("# body", encoding="utf-8")
        ts = base + _dt.timedelta(hours=offset)
        ns = int(ts.timestamp() * 1_000_000_000)
        os.utime(path, ns=(ns, ns))
    server = _build(tmp_path)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("recent_pages", {})
    names = [row["name"] for row in result.structured_content["result"]]
    assert names == ["c.md", "b.md", "a.md"]


@pytest.mark.asyncio
async def test_recent_pages_truncates_to_limit(tmp_path: Path) -> None:
    """``limit`` truncates the result; ``limit=0`` returns empty."""
    import os

    base = _dt.datetime(2026, 1, 1, 12, 0, 0, tzinfo=_dt.timezone.utc)
    for offset, name in enumerate(["a.md", "b.md", "c.md"]):
        path = tmp_path / name
        path.write_text("# body", encoding="utf-8")
        ts = base + _dt.timedelta(hours=offset)
        ns = int(ts.timestamp() * 1_000_000_000)
        os.utime(path, ns=(ns, ns))
    server = _build(tmp_path)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("recent_pages", {"limit": 2})
        names = [row["name"] for row in result.structured_content["result"]]
        assert names == ["c.md", "b.md"]
        # ``limit=0`` returns the empty list (not an error).
        result = await client.call_tool("recent_pages", {"limit": 0})
        assert result.is_error is False
        assert result.structured_content == {"result": []}


@pytest.mark.asyncio
async def test_recent_pages_carries_name_mtime_iso_size_bytes(tmp_path: Path) -> None:
    """Each entry has the three fields ``recent_pages`` documents."""
    path = tmp_path / "note.md"
    path.write_text("hello world", encoding="utf-8")
    server = _build(tmp_path)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("recent_pages", {})
    [row] = result.structured_content["result"]
    assert row["name"] == "note.md"
    assert row["size_bytes"] == len("hello world")
    # ``mtime_iso`` is an ISO-8601 string ending in ``+00:00``.
    assert isinstance(row["mtime_iso"], str)
    assert row["mtime_iso"].endswith("+00:00")


@pytest.mark.asyncio
async def test_recent_pages_prefix_filters_to_subtree(tmp_path: Path) -> None:
    import os

    base = _dt.datetime(2026, 1, 1, 12, 0, 0, tzinfo=_dt.timezone.utc)
    for offset, rel in enumerate(
        ["Daily/2026-01-05.md", "Daily/2026-01-06.md", "Library/static.md"]
    ):
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# body", encoding="utf-8")
        ts = base + _dt.timedelta(hours=offset)
        ns = int(ts.timestamp() * 1_000_000_000)
        os.utime(path, ns=(ns, ns))
    server = _build(tmp_path)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("recent_pages", {"prefix": "Daily"})
    names = [row["name"] for row in result.structured_content["result"]]
    assert names == ["Daily/2026-01-06.md", "Daily/2026-01-05.md"]


@pytest.mark.asyncio
async def test_recent_pages_skips_hidden_directories(tmp_path: Path) -> None:
    """``.git/HEAD.md`` and friends don't leak into the recent list."""
    visible = tmp_path / "Daily/2026-01-05.md"
    visible.parent.mkdir(parents=True, exist_ok=True)
    visible.write_text("# visible", encoding="utf-8")
    noise = tmp_path / ".git/HEAD.md"
    noise.parent.mkdir(parents=True, exist_ok=True)
    noise.write_text("# noise", encoding="utf-8")
    server = _build(tmp_path)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("recent_pages", {})
    names = [row["name"] for row in result.structured_content["result"]]
    assert names == ["Daily/2026-01-05.md"]

"""Layer-1 tests for ``pages_touching_topic`` (T12).

The T10 skeleton-error test for ``pages_touching_topic`` (asserting
the tool raised ``ToolError("… T12")`` on every call) was inverted in
``tests/test_journal_gate.py``: now the tool boots cleanly and
returns an empty list against an empty ``tmp_path``. The real
behaviors — name match, content match, match-kind (``name`` /
``content`` / ``both``), snippet shaping, prefix filtering, hidden-
dir skip, prefix-rejection — live here.

Each test constructs an in-memory MCP server whose
``JournalConfig.enabled`` points at a ``tmp_path`` populated with
synthetic ``*.md`` files. The default code path is the **Python
fallback**: the test fixture monkeypatches
``mcp_silverbullet.journal._RG_BIN`` to ``""`` so ``_rg_available()``
returns False and ``_pages_touching_topic`` reads every body. A
dedicated suite (``test_pages_touching_topic_with_rg_path``)
exercises the ``rg --json`` branch when ``rg`` is on PATH
(this dev box has it).

The query is treated as a literal substring: a query of ``.*`` does
not activate regex syntax in either path. Newlines / extra whitespace
in the query are collapsed to a single space before matching.
"""

from __future__ import annotations

import os
from pathlib import Path

import httpx2 as httpx
import pytest
from mcp.client import Client

import mcp_silverbullet.journal as journal_mod
from mcp_silverbullet.journal import JournalConfig
from mcp_silverbullet.sb_client import SBClient
from mcp_silverbullet.server import build_mcp


TOKEN = "test-secret-do-not-use-in-prod"
RESOURCE_URL = "http://bridge.test/mcp"


@pytest.fixture
def force_python_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the Python fallback path regardless of whether ``rg`` is on PATH.

    Tests run on dev boxes that may or may not have ``rg``. Pinning
    ``_RG_BIN`` to the empty string short-circuits ``_rg_available``
    and exercises the same code path the Layer-1 test on a stripped
    runtime image would take.
    """
    monkeypatch.setattr(journal_mod, "_RG_BIN", "")


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


def _names(result) -> list[str]:
    return [row["name"] for row in result.structured_content["result"]]


# --- input validation --------------------------------------------------


@pytest.mark.asyncio
async def test_pages_touching_topic_rejects_empty_query(
    tmp_path: Path, force_python_path: None
) -> None:
    """Empty query → ``ToolError`` (would match every file; UX footgun)."""
    server = _build(tmp_path)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("pages_touching_topic", {"query": ""})
    assert result.is_error is True
    assert "empty" in result.content[0].text.lower()


@pytest.mark.asyncio
async def test_pages_touching_topic_rejects_whitespace_only_query(
    tmp_path: Path, force_python_path: None
) -> None:
    """``"   "`` / ``"\\n\\t  "`` → ``ToolError`` after whitespace collapse."""
    server = _build(tmp_path)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "pages_touching_topic", {"query": "   \n\t  "}
        )
    assert result.is_error is True
    assert "empty" in result.content[0].text.lower()


@pytest.mark.asyncio
async def test_pages_touching_topic_rejects_dot_dot_prefix(
    tmp_path: Path, force_python_path: None
) -> None:
    """Prefix-traversal guard from ``_validate_prefix`` is wired through."""
    server = _build(tmp_path)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "pages_touching_topic",
            {"query": "anything", "prefix": "../etc"},
        )
    assert result.is_error is True
    assert ".." in result.content[0].text


@pytest.mark.asyncio
async def test_pages_touching_topic_rejects_absolute_prefix(
    tmp_path: Path, force_python_path: None
) -> None:
    """Absolute ``prefix`` is rejected before any FS call."""
    server = _build(tmp_path)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "pages_touching_topic",
            {"query": "anything", "prefix": "/etc"},
        )
    assert result.is_error is True
    assert "/" in result.content[0].text


# --- empty / no-match --------------------------------------------------


@pytest.mark.asyncio
async def test_pages_touching_topic_empty_space_returns_empty(
    tmp_path: Path, force_python_path: None
) -> None:
    server = _build(tmp_path)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "pages_touching_topic", {"query": "anything"}
        )
    assert result.is_error is False
    # ``list[X]`` returns wrap in ``{"result": [...]}`` (T11 carry-forward).
    assert result.structured_content == {"result": []}


@pytest.mark.asyncio
async def test_pages_touching_topic_no_match_returns_empty(
    tmp_path: Path, force_python_path: None
) -> None:
    """Files exist; the query matches neither name nor body."""
    _write(tmp_path, "a.md", "alpha\n")
    _write(tmp_path, "b.md", "beta\n")
    server = _build(tmp_path)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "pages_touching_topic", {"query": "zzz"}
        )
    assert result.structured_content == {"result": []}


# --- name match --------------------------------------------------------


@pytest.mark.asyncio
async def test_pages_touching_topic_matches_name_only(
    tmp_path: Path, force_python_path: None
) -> None:
    """``"alpha"`` matches ``alpha-notes.md`` (name) but body has nothing.

    The match kind is ``"name"`` and the snippet is the body excerpt
    (the page's first prose line after any frontmatter is stripped).
    The unrelated ``beta.md`` body has no occurrence of ``alpha`` so
    it doesn't surface.
    """
    _write(tmp_path, "alpha-notes.md", "Body about something else entirely.\n")
    _write(tmp_path, "beta.md", "Body with no occurrence of the query at all.\n")
    server = _build(tmp_path)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "pages_touching_topic", {"query": "alpha"}
        )
    assert result.is_error is False
    payload = result.structured_content["result"]
    assert len(payload) == 1
    [row] = payload
    assert row["name"] == "alpha-notes.md"
    assert row["match"] == "name"
    # Name-only snippet is a body excerpt (frontmatter stripped).
    assert row["snippet"] == "Body about something else entirely."


@pytest.mark.asyncio
async def test_pages_touching_topic_name_match_against_relative_path(
    tmp_path: Path, force_python_path: None
) -> None:
    """``"Daily"`` matches ``Daily/2026-01-05.md`` (name match against relative path).

    The ticket's done-when says the name match is against the
    basename, but the operator's clear intent (verified by the
    ``query="DAILY"`` example in the same paragraph) is to match the
    relative path so daily-journal subdirectory pages show up.
    """
    _write(tmp_path, "Daily/2026-01-05.md", "Today I shipped something.\n")
    _write(tmp_path, "Library/static.md", "old reference page\n")
    server = _build(tmp_path)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "pages_touching_topic", {"query": "Daily"}
        )
    payload = result.structured_content["result"]
    assert len(payload) == 1
    assert payload[0]["name"] == "Daily/2026-01-05.md"
    assert payload[0]["match"] == "name"


@pytest.mark.asyncio
async def test_pages_touching_topic_inverted_case_finds_daily_directory(
    tmp_path: Path, force_python_path: None
) -> None:
    """The ticket's done-when: ``query="DAILY"`` matches every Daily/*.md
    by name, plus any page whose body mentions "daily".

    ``Areas/Daily Notes.md`` body has "no match at all" so it's a
    pure name match. The two ``Daily/*.md`` files also have no
    "daily" in their bodies, so they're name-only too.
    """
    _write(tmp_path, "Daily/2026-01-05.md", "Today I shipped something.\n")
    _write(tmp_path, "Daily/2026-02-09.md", "Other day's note.\n")
    _write(tmp_path, "Areas/Daily Notes.md", "no match at all in body\n")
    _write(tmp_path, "Library/daily-journal-aggregator.md", "the bridge parsed the daily dump\n")
    _write(tmp_path, "Library/static.md", "old reference page\n")
    server = _build(tmp_path)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "pages_touching_topic", {"query": "DAILY"}
        )
    names = sorted(_names(result))
    assert names == [
        "Areas/Daily Notes.md",
        "Daily/2026-01-05.md",
        "Daily/2026-02-09.md",
        "Library/daily-journal-aggregator.md",
    ]
    # The two Daily/*.md files and Areas/Daily Notes.md have no body
    # match → pure "name". The aggregator has both a name match and a
    # body match (body contains "daily") → "both".
    kinds = {row["name"]: row["match"] for row in result.structured_content["result"]}
    assert kinds == {
        "Areas/Daily Notes.md": "name",
        "Daily/2026-01-05.md": "name",
        "Daily/2026-02-09.md": "name",
        "Library/daily-journal-aggregator.md": "both",
    }


# --- content match -----------------------------------------------------


@pytest.mark.asyncio
async def test_pages_touching_topic_matches_content_only(
    tmp_path: Path, force_python_path: None
) -> None:
    """Body contains query; filename does not. ``match="content"``."""
    _write(tmp_path, "totally-unrelated.md", "The bridge shipped today.\n")
    server = _build(tmp_path)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "pages_touching_topic", {"query": "bridge"}
        )
    payload = result.structured_content["result"]
    assert len(payload) == 1
    [row] = payload
    assert row["name"] == "totally-unrelated.md"
    assert row["match"] == "content"
    # Snippet centers on the match within the line.
    assert "bridge" in row["snippet"]


@pytest.mark.asyncio
async def test_pages_touching_topic_matches_both_name_and_content(
    tmp_path: Path, force_python_path: None
) -> None:
    """Same query hits name AND body of one file → ``match="both"``."""
    _write(tmp_path, "bridge.md", "Today the bridge was first wired up.\n")
    server = _build(tmp_path)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "pages_touching_topic", {"query": "bridge"}
        )
    payload = result.structured_content["result"]
    assert len(payload) == 1
    [row] = payload
    assert row["match"] == "both"
    # When both match, the snippet comes from the content finding
    # (not the body excerpt used for name-only matches).
    assert "bridge" in row["snippet"]


# --- snippet shape -----------------------------------------------------


@pytest.mark.asyncio
async def test_pages_touching_topic_snippet_short_line_returns_line_verbatim(
    tmp_path: Path, force_python_path: None
) -> None:
    """A line shorter than ``_SNIPPET_MAX_LEN`` is returned without ellipses."""
    _write(
        tmp_path,
        "short.md",
        "Bridge shipped today; ceremony at 4pm.\n",
    )
    server = _build(tmp_path)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "pages_touching_topic", {"query": "shipped"}
        )
    [row] = result.structured_content["result"]
    assert row["snippet"] == "Bridge shipped today; ceremony at 4pm."
    assert "…" not in row["snippet"]


@pytest.mark.asyncio
async def test_pages_touching_topic_snippet_long_line_is_windowed_with_ellipses(
    tmp_path: Path, force_python_path: None
) -> None:
    """A line longer than ``_SNIPPET_MAX_LEN`` is windowed to 80 chars,
    centered on the match, with leading/trailing ellipses."""
    long_line = (
        "lorem ipsum dolor sit amet consectetur adipiscing "
        "elit sed do eiusmod tempor incididunt ut labore et "
        "dolore magna aliqua ut enim ad minim veniam quis "
        "nostrud exercitation ullamco laboris nisi ut aliquip "
        "ex ea commodo consequat the KEYWORD here duis aute "
        "irure dolor in reprehenderit in voluptate velit esse"
    )
    assert len(long_line) > 80
    _write(tmp_path, "long.md", long_line + "\n")
    server = _build(tmp_path)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "pages_touching_topic", {"query": "KEYWORD"}
        )
    [row] = result.structured_content["result"]
    snippet = row["snippet"]
    # Length bounded by max + ellipses.
    assert len(snippet) <= 80 + 4, snippet  # 2 for "… ", 2 for " …"
    assert "KEYWORD" in snippet
    # At least one ellipsis on a 250+ char line.
    assert "…" in snippet


@pytest.mark.asyncio
async def test_pages_touching_topic_snippet_picks_correct_line(
    tmp_path: Path, force_python_path: None
) -> None:
    """When the body has multiple lines, the snippet picks the one with the match."""
    _write(
        tmp_path,
        "many.md",
        (
            "first line, no match here\n"
            "second line, also no match\n"
            "third line, finally has the marker token\n"
            "fourth line\n"
        ),
    )
    server = _build(tmp_path)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "pages_touching_topic", {"query": "marker"}
        )
    [row] = result.structured_content["result"]
    assert row["snippet"] == "third line, finally has the marker token"


@pytest.mark.asyncio
async def test_pages_touching_topic_body_excerpt_strips_frontmatter(
    tmp_path: Path, force_python_path: None
) -> None:
    """Name-only snippet is a body excerpt with frontmatter stripped."""
    _write(
        tmp_path,
        "fronmatter-name.md",
        "---\ntags: meta\nauthor: anon\n---\n\nActual content here.\n",
    )
    server = _build(tmp_path)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "pages_touching_topic",
            {"query": "fronmatter"},  # matches the name, not body
        )
    [row] = result.structured_content["result"]
    assert row["match"] == "name"
    assert row["snippet"] == "Actual content here."


# --- multi-result ordering --------------------------------------------


@pytest.mark.asyncio
async def test_pages_touching_topic_results_sorted_by_name(
    tmp_path: Path, force_python_path: None
) -> None:
    """Multiple matches → ``name``-ascending order (deterministic)."""
    _write(tmp_path, "zeta.md", "alpha\n")
    _write(tmp_path, "alpha.md", "alpha\n")
    _write(tmp_path, "mike.md", "alpha\n")
    server = _build(tmp_path)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "pages_touching_topic", {"query": "alpha"}
        )
    assert _names(result) == ["alpha.md", "mike.md", "zeta.md"]


# --- prefix filtering --------------------------------------------------


@pytest.mark.asyncio
async def test_pages_touching_topic_prefix_filters_to_subtree(
    tmp_path: Path, force_python_path: None
) -> None:
    """``prefix="Areas"`` restricts the inventory to paths containing ``"Areas"``."""
    _write(tmp_path, "Daily/2026-01-05.md", "bridge was wired\n")
    _write(tmp_path, "Areas/Contacts/c.md", "bridge mentioned\n")
    _write(tmp_path, "Library/static.md", "no mention here\n")
    server = _build(tmp_path)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "pages_touching_topic",
            {"query": "bridge", "prefix": "Areas"},
        )
    payload = result.structured_content["result"]
    assert [row["name"] for row in payload] == ["Areas/Contacts/c.md"]


# --- hidden-directory skip --------------------------------------------


@pytest.mark.asyncio
async def test_pages_touching_topic_skips_hidden_directories(
    tmp_path: Path, force_python_path: None
) -> None:
    """``.git/…`` etc. don't leak into search results (carried from ``_iter_md``)."""
    _write(tmp_path, "Daily/2026-01-05.md", "bridge\n")
    _write(tmp_path, ".git/HEAD.md", "bridge in a hidden dir\n")
    _write(tmp_path, ".cache/index.md", "another bridge\n")
    server = _build(tmp_path)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "pages_touching_topic", {"query": "bridge"}
        )
    payload = result.structured_content["result"]
    assert [row["name"] for row in payload] == ["Daily/2026-01-05.md"]


# --- literal-substring semantics --------------------------------------


@pytest.mark.asyncio
async def test_pages_touching_topic_treats_query_as_literal_substring(
    tmp_path: Path, force_python_path: None
) -> None:
    """Regex metacharacters in the query are matched literally, not as regex."""
    _write(tmp_path, "a.md", "the pattern .* matches everything\n")
    _write(tmp_path, "b.md", "body mentions a period and a star like this: .* literally\n")
    _write(tmp_path, "c.md", "no special chars here\n")
    server = _build(tmp_path)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "pages_touching_topic", {"query": ".*"}
        )
    # Both ``a.md`` and ``b.md`` contain the literal substring ``".*"``;
    # ``c.md`` does not. None of them is treated as a regex wildcard.
    payload = result.structured_content["result"]
    assert sorted(row["name"] for row in payload) == ["a.md", "b.md"]


@pytest.mark.asyncio
async def test_pages_touching_topic_collapses_query_whitespace(
    tmp_path: Path, force_python_path: None
) -> None:
    """Internal whitespace in the query collapses before matching."""
    _write(tmp_path, "a.md", "the bridge is up\n")
    server = _build(tmp_path)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "pages_touching_topic", {"query": "  the\tbridge\nis "}
        )
    [row] = result.structured_content["result"]
    assert row["name"] == "a.md"


# --- rg path (optional accel) ------------------------------------------


@pytest.mark.asyncio
async def test_pages_touching_topic_uses_rg_when_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When ``_RG_BIN`` is set, the rg-acceleration path is exercised.

    Asserting on the rg path is indirect: we monkeypatch the
    memoization cache to point at the system ``rg`` and verify the
    same set of matches comes back as the Python path did for the
    same corpus. (If ``rg`` isn't installed in the test env the test
    skips — the Python path is covered by the rest of this file.)
    """
    rg = shutil_which("rg")
    if rg is None:
        pytest.skip("rg not on PATH in this test env")
    monkeypatch.setattr(journal_mod, "_RG_BIN", rg)
    _write(tmp_path, "Daily/2026-01-05.md", "the bridge is up\n")
    _write(tmp_path, "Library/static.md", "no match here\n")
    _write(tmp_path, "Areas/Notes.md", "bridge also mentioned\n")
    server = _build(tmp_path)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "pages_touching_topic", {"query": "bridge"}
        )
    payload = result.structured_content["result"]
    # Same hits as the Python path: name match on ``Daily/…``,
    # content-only on ``Library/static.md`` is absent (no bridge),
    # and content-only on ``Areas/Notes.md``.
    assert sorted(row["name"] for row in payload) == [
        "Areas/Notes.md",
        "Daily/2026-01-05.md",
    ]
    # Snippets still come through (rg path uses them for content match).
    for row in payload:
        assert "snippet" in row


def shutil_which(name: str) -> str | None:
    """Local import shim so the test file's import block stays clean."""
    import shutil

    return shutil.which(name)


@pytest.mark.asyncio
async def test_pages_touching_topic_falls_back_when_rg_times_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hanging ``rg`` subprocess falls back to the Python path.

    We don't actually hang rg (that would slow the suite); we
    monkeypatch ``subprocess.run`` to raise ``TimeoutExpired``
    immediately and verify the same hits come back. This proves the
    ``except subprocess.TimeoutExpired`` branch is wired.
    """
    rg = shutil_which("rg")
    if rg is None:
        pytest.skip("rg not on PATH in this test env")
    monkeypatch.setattr(journal_mod, "_RG_BIN", rg)

    import subprocess as _sp

    def _raise_timeout(*args, **kwargs):
        raise _sp.TimeoutExpired(cmd=args[0] if args else "rg", timeout=0.001)

    monkeypatch.setattr(journal_mod.subprocess, "run", _raise_timeout)
    _write(tmp_path, "a.md", "bridge\n")
    server = _build(tmp_path)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "pages_touching_topic", {"query": "bridge"}
        )
    [row] = result.structured_content["result"]
    assert row["name"] == "a.md"
    assert row["match"] == "content"


@pytest.mark.asyncio
async def test_pages_touching_topic_falls_back_when_rg_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-zero (other than ``1`` = no-matches) ``rg`` exit falls back to Python.

    We simulate ``rg`` writing junk to stdout by returning a non-zero
    exit code; the bridge must log a warning and continue with the
    Python scan rather than surface the error to the operator.
    """
    rg = shutil_which("rg")
    if rg is None:
        pytest.skip("rg not on PATH in this test env")
    monkeypatch.setattr(journal_mod, "_RG_BIN", rg)

    import subprocess as _sp

    def _fake_run_fail(*args, **kwargs):
        # ``capture_output`` and ``text`` are passed by ``_rg_content_matches``.
        return _sp.CompletedProcess(
            args=["rg"], returncode=2, stdout="", stderr="rg: boom"
        )

    monkeypatch.setattr(journal_mod.subprocess, "run", _fake_run_fail)
    _write(tmp_path, "a.md", "bridge\n")
    server = _build(tmp_path)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "pages_touching_topic", {"query": "bridge"}
        )
    [row] = result.structured_content["result"]
    assert row["name"] == "a.md"


# --- wire shape (carried from T11) ------------------------------------


@pytest.mark.asyncio
async def test_pages_touching_topic_list_payload_is_wrapped(
    tmp_path: Path, force_python_path: None
) -> None:
    """``list[X]`` returns wrap in ``{"result": [...]}`` (T11 SDK-shape carry-forward)."""
    _write(tmp_path, "a.md", "bridge\n")
    server = _build(tmp_path)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "pages_touching_topic", {"query": "bridge"}
        )
    payload = result.structured_content
    assert isinstance(payload, dict)
    assert list(payload.keys()) == ["result"]
    assert isinstance(payload["result"], list)
    row = payload["result"][0]
    # Each row is the three documented keys only.
    assert set(row.keys()) == {"name", "match", "snippet"}
    assert row["match"] in {"name", "content", "both"}


# --- filesystem safety -------------------------------------------------


@pytest.mark.asyncio
async def test_pages_touching_topic_skips_files_that_disappear_between_walk_and_read(
    tmp_path: Path, force_python_path: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file removed mid-iteration is skipped, not surfaced as an error.

    We don't actually remove a file (that's racy); we monkeypatch
    ``_safe_read_body`` to return ``None`` once and verify the
    surviving match is still surfaced.
    """
    _write(tmp_path, "a.md", "bridge\n")
    _write(tmp_path, "b.md", "bridge too\n")

    real_read = journal_mod._safe_read_body
    call_count = {"n": 0}

    def _read_once_then_fail(path: Path):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return None  # simulate the first file vanishing
        return real_read(path)

    monkeypatch.setattr(journal_mod, "_safe_read_body", _read_once_then_fail)
    server = _build(tmp_path)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "pages_touching_topic", {"query": "bridge"}
        )
    [row] = result.structured_content["result"]
    assert row["name"] in {"a.md", "b.md"}

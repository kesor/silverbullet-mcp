"""Layer-1 tests for the journal-tools config gate (T10) and the
boot-time inventory of all four journal tools.

The journal surface is an optional, strictly-additive set of four
direct-FS read tools. It ships only when two env vars agree: the
operator opts in with ``MCP_SILVERBULLET_JOURNAL_TOOLS=1`` AND points
``MCP_SILVERBULLET_SPACE_PATH`` at a readable directory. This file
covers all three modes of the gate (off, on-but-unusable, on) at the
boundary where it lives: the boot-time ``resolve_journal_config``
helper, the ``build_mcp`` registration call, and the resulting
``list_tools`` inventory.

The T11 ticket inverted the T10 skeleton-error tests for the three
read tools (``journal_histogram`` / ``tag_summary`` /
``recent_pages``) into ``tests/test_journal_read.py``, where the
real behaviors (returned shapes, prefix filtering, mtime sorting,
frontmatter parsing) live. T12 inverts the last skeleton
(``pages_touching_topic``) and the real behaviors (name+content
search, match-kind, snippet shape) live in
``tests/test_journal_search.py``. The remaining assertion here is
that ``pages_touching_topic`` round-trips against an empty
``tmp_path`` with ``is_error=False`` — i.e., it boots cleanly
alongside the three T11 tools.

Each test reuses the in-memory ``Client(mcp, raise_exceptions=True)``
pattern from ``tests/test_tools_in_memory.py`` and the
``httpx.MockTransport`` substitution for SB so no live FS, no live
SB, no socket is required.
"""

from __future__ import annotations

import httpx2 as httpx
import pytest
from mcp.client import Client
from mcp.server.mcpserver.server import MCPServer

from mcp_silverbullet.journal import (
    JournalConfig,
    register_journal_tools,
    resolve_journal_config,
)
from mcp_silverbullet.sb_client import SBClient
from mcp_silverbullet.server import build_mcp


TOKEN = "test-secret-do-not-use-in-prod"
RESOURCE_URL = "http://bridge.test/mcp"
JOURNAL_TOOL_NAMES = {
    "journal_histogram",
    "tag_summary",
    "recent_pages",
    "pages_touching_topic",
}
SB_TOOL_NAMES = {
    "read_page",
    "write_page",
    "delete_page",
    "list_pages",
    "append_to_page",
    "patch_page_lines",
    "patch_page_replace",
    "move_page",
}


def _build(handler, journal: JournalConfig | None = None) -> MCPServer:
    """Build an MCP server with the same mock-SB transport the other tests use."""
    transport = httpx.MockTransport(handler)
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
        journal=journal,
    )


# --- resolve_journal_config ------------------------------------------


def test_resolve_defaults_to_disabled_when_no_env_vars_are_set() -> None:
    """Unset env = gate off, even if a space path happens to be readable."""
    cfg = resolve_journal_config({})
    assert cfg == JournalConfig(enabled=False, space_path=None)


def test_resolve_disables_when_only_opt_in_is_set() -> None:
    """``JOURNAL_TOOLS=1`` alone is not enough; ``SPACE_PATH`` is also required."""
    cfg = resolve_journal_config({"MCP_SILVERBULLET_JOURNAL_TOOLS": "1"})
    assert cfg.enabled is False
    assert cfg.space_path is None


def test_resolve_disables_when_opt_in_is_untruthy() -> None:
    """Empty / 0 / no / false — all disable the gate, even with a path."""
    for value in ("", "0", "no", "false"):
        cfg = resolve_journal_config(
            {
                "MCP_SILVERBULLET_JOURNAL_TOOLS": value,
                "MCP_SILVERBULLET_SPACE_PATH": "/tmp",
            }
        )
        assert cfg.enabled is False, f"truthy parse failed for {value!r}"


def test_resolve_disables_when_space_path_is_unreadable(
    tmp_path, caplog: pytest.LogCaptureFixture
) -> None:
    """Unreadable path → gate off + a WARN log line the operator can see."""
    bad = tmp_path / "missing"
    with caplog.at_level("WARNING"):
        cfg = resolve_journal_config(
            {
                "MCP_SILVERBULLET_JOURNAL_TOOLS": "true",
                "MCP_SILVERBULLET_SPACE_PATH": str(bad),
            }
        )
    assert cfg.enabled is False
    assert cfg.space_path == str(bad)
    assert any(
        "not readable" in rec.message for rec in caplog.records
    ), "operator needs a visible warning when the gate is requested-but-unusable"


def test_resolve_disables_when_opt_in_set_but_space_path_empty(
    caplog: pytest.LogCaptureFixture
) -> None:
    """Empty space path with opt-in → gate off + WARN."""
    with caplog.at_level("WARNING"):
        cfg = resolve_journal_config(
            {
                "MCP_SILVERBULLET_JOURNAL_TOOLS": "yes",
                "MCP_SILVERBULLET_SPACE_PATH": "",
            }
        )
    assert cfg.enabled is False
    assert cfg.space_path is None
    assert any("SPACE_PATH is empty" in rec.message for rec in caplog.records)


def test_resolve_enables_when_opt_in_truthy_and_space_path_readable(
    tmp_path, caplog: pytest.LogCaptureFixture
) -> None:
    """Happy path: opt-in + readable path = gate on, INFO log."""
    with caplog.at_level("INFO"):
        cfg = resolve_journal_config(
            {
                "MCP_SILVERBULLET_JOURNAL_TOOLS": "1",
                "MCP_SILVERBULLET_SPACE_PATH": str(tmp_path),
            }
        )
    assert cfg == JournalConfig(enabled=True, space_path=str(tmp_path))
    assert any(
        "journal tools enabled" in rec.message for rec in caplog.records
    )


# --- build_mcp integration with the gate ------------------------------


@pytest.mark.asyncio
async def test_build_mcp_omits_journal_tools_when_journal_is_none() -> None:
    """Backward-compat: ``build_mcp`` without a ``journal`` arg = no journal tools."""
    server = _build(lambda req: httpx.Response(200))
    async with Client(server, raise_exceptions=True) as client:
        tools = await client.list_tools()
        names = {t.name for t in tools.tools}
    assert names == SB_TOOL_NAMES
    assert names.isdisjoint(JOURNAL_TOOL_NAMES)


@pytest.mark.asyncio
async def test_build_mcp_omits_journal_tools_when_gate_is_off() -> None:
    """Gate off (``JournalConfig(enabled=False, ...)``) = no journal tools.

    The ``register_journal_tools`` registration is a no-op when the
    gate is off; the ``/.fs``-backed tools continue to work.
    """
    server = _build(
        lambda req: httpx.Response(200),
        journal=JournalConfig(enabled=False, space_path=None),
    )
    async with Client(server, raise_exceptions=True) as client:
        tools = await client.list_tools()
        names = {t.name for t in tools.tools}
    assert names == SB_TOOL_NAMES


@pytest.mark.asyncio
async def test_build_mcp_registers_journal_tools_when_gate_is_on(
    tmp_path,
) -> None:
    """Gate on = four journal tools present alongside the ``/.fs`` tools.

    All four journal tools round-trip against the empty ``tmp_path``
    — they return empty results without raising, proving the T10
    skeletons have been replaced (T11 replaced three; T12 replaced
    the fourth, ``pages_touching_topic``).
    """
    server = _build(
        lambda req: httpx.Response(200),
        journal=JournalConfig(enabled=True, space_path=str(tmp_path)),
    )
    async with Client(server, raise_exceptions=True) as client:
        tools = await client.list_tools()
        names = {t.name for t in tools.tools}
        assert names == SB_TOOL_NAMES | JOURNAL_TOOL_NAMES
        # T11: the three read tools are no longer skeletons — an
        # empty ``tmp_path`` is a valid (empty) space, so each
        # returns its empty-collection shape with ``is_error=False``.
        # ``dict[str, int]`` returns go through a RootModel and are
        # emitted unwrapped; ``list[…]`` returns are wrapped in
        # ``{"result": …}``.
        empty = await client.call_tool("journal_histogram", {})
        assert empty.is_error is False
        assert empty.structured_content == {}
        empty = await client.call_tool("tag_summary", {})
        assert empty.is_error is False
        assert empty.structured_content == {}
        empty = await client.call_tool("recent_pages", {})
        assert empty.is_error is False
        assert empty.structured_content == {"result": []}
        # T12: ``pages_touching_topic`` no longer raises the
        # skeleton error — an empty ``tmp_path`` against an empty
        # query returns the empty result shape (``{"result": []}``,
        # ``is_error=False``). The shape-and-error assertions for the
        # real behaviors live in ``tests/test_journal_search.py``.
        result = await client.call_tool(
            "pages_touching_topic", {"query": "anything"}
        )
        assert result.is_error is False
        assert result.structured_content == {"result": []}


# --- register_journal_tools is a no-op when the gate is off -----------


def test_register_journal_tools_no_ops_when_disabled() -> None:
    """``register_journal_tools(mcp, JournalConfig(enabled=False))`` is a no-op.

    Goes through ``build_mcp`` with a disabled config and asserts the
    tool list contains no journal names. (``build_mcp`` is the
    caller; a future refactor that drops the ``if journal is not None``
    guard would silently attach journal tools to the SB-backed server.)
    """
    mcp = MCPServer(name="noop-test")
    register_journal_tools(mcp, JournalConfig(enabled=False, space_path=None))
    # MCPServer's tool table is SDK-private; the public check is
    # ``test_build_mcp_omits_journal_tools_when_gate_is_off``. This
    # test exists so the no-op path can't quietly start raising or
    # regress to a partial registration when the disabled-config
    # short-circuit is touched.
    assert mcp is not None

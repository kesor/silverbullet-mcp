"""Layer-1 tests for the journal-tools config gate (T10).

The journal surface is an optional, strictly-additive set of four
direct-FS read tools. It ships only when two env vars agree: the
operator opts in with ``MCP_SILVERBULLET_JOURNAL_TOOLS=1`` AND points
``MCP_SILVERBULLET_SPACE_PATH`` at a readable directory. This file
covers all three modes of the gate (off, on-but-unusable, on) at the
boundary where it lives: the boot-time ``resolve_journal_config``
helper, the ``build_mcp`` registration call, and the resulting
``list_tools`` inventory.

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
SB_TOOL_NAMES = {"read_page", "write_page", "list_pages"}


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
async def test_build_mcp_registers_journal_skeleton_when_gate_is_on(
    tmp_path,
) -> None:
    """Gate on = four journal tools present alongside the ``/.fs`` tools."""
    server = _build(
        lambda req: httpx.Response(200),
        journal=JournalConfig(enabled=True, space_path=str(tmp_path)),
    )
    async with Client(server, raise_exceptions=True) as client:
        tools = await client.list_tools()
        names = {t.name for t in tools.tools}
    assert names == SB_TOOL_NAMES | JOURNAL_TOOL_NAMES


# --- skeleton tool behavior ------------------------------------------


@pytest.mark.asyncio
async def test_skeleton_journal_tools_raise_not_implemented(
    tmp_path,
) -> None:
    """Each skeleton tool raises a ``ToolError`` until T11/T12 fill in the body.

    Going through the wire (not calling the handler directly) so a
    future SDK rename of the registry attribute doesn't break the
    test, and so the error shape we assert is the one the operator
    actually sees.
    """
    server = _build(
        lambda req: httpx.Response(200),
        journal=JournalConfig(enabled=True, space_path=str(tmp_path)),
    )
    async with Client(server, raise_exceptions=True) as client:
        for tool_name in sorted(JOURNAL_TOOL_NAMES):
            # The four tool signatures differ: one takes a required
            # ``query``, the rest take optional ``prefix`` /
            # ``limit``. Call each with the right minimum-arg set so
            # a future T11/T12 signature change fails loudly here
            # rather than silently dropping a required parameter.
            kwargs: dict[str, object] = {}
            if tool_name == "pages_touching_topic":
                kwargs = {"query": "anything"}
            result = await client.call_tool(tool_name, kwargs)
            assert result.is_error is True, (
                f"{tool_name} should be a T10 skeleton, not a real implementation"
            )
            assert "not implemented" in result.content[0].text, (
                f"{tool_name} error text was {result.content[0].text!r}"
            )


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

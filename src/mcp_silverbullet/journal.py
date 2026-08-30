"""Journal surface — direct-FS read tools, gated by config.

T10 of the map. The bridge may run on a host that does *not* have
direct access to the SB space directory (e.g., a sidecar container
without a volume mount); the journal tools are an optional,
strictly-additive surface that requires ``MCP_SILVERBULLET_SPACE_PATH``
and ``MCP_SILVERBULLET_JOURNAL_TOOLS`` to enable. With either unset or
the path unreadable, the bridge boots cleanly without the journal
tools and the existing ``/.fs``-backed tools continue to work.

Two-step gate (resolved at :func:`resolve_journal_config`):

1. ``MCP_SILVERBULLET_JOURNAL_TOOLS`` is truthy — otherwise the gate
   is off and we skip every other check (operator did not opt in).
2. ``MCP_SILVERBULLET_SPACE_PATH`` is a non-empty path AND
   ``os.access(path, os.R_OK)`` — otherwise the gate is off and we
   log a one-line WARN.

The skeleton in this module registers four placeholder tools when
the gate is on; T11 (histogram / tag_summary / recent_pages) and T12
(``pages_touching_topic``) replace the bodies with real
implementations. Until then each handler raises a :exc:`ToolError`
so a stray call surfaces loudly instead of silently returning empty
data.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

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


def _not_implemented(ticket: str) -> None:
    """Placeholder body for the four journal tools until T11/T12 land."""
    raise ToolError(
        f"journal tool not implemented yet; landing in {ticket}"
    )


def register_journal_tools(
    mcp: MCPServer,
    config: JournalConfig,
) -> None:
    """Register the four journal tools iff the gate is on.

    Called from :func:`mcp_silverbullet.server.build_mcp`; the caller
    passes the already-resolved :class:`JournalConfig` so this
    function does no env parsing and is pure against its inputs. When
    the gate is off this is a no-op (the ``/.fs``-backed tools
    continue to work; nothing about the journal surface leaks into the
    tool list).
    """
    if not config.enabled:
        return

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
        _not_implemented("T11")

    @mcp.tool(
        title="Tag summary",
        description=(
            "Count occurrences of every value under `tags:` in the YAML "
            "frontmatter of `*.md` pages under the SB space directory. "
            "Restricted to pages whose relative path contains `prefix`."
        ),
    )
    async def tag_summary(prefix: str = "") -> dict[str, int]:
        _not_implemented("T11")

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
    ) -> list[dict[str, str | int]]:
        _not_implemented("T11")

    @mcp.tool(
        title="Pages touching topic",
        description=(
            "Search `*.md` pages under the SB space directory by both "
            "name and body, case-insensitive substring. Returns one "
            "entry per match with `name`, the kind of match (`name`, "
            "`content`, or `both`), and a short Markdown-shaped snippet "
            "around the body match. Restricted to pages whose relative "
            "path contains `prefix`."
        ),
    )
    async def pages_touching_topic(
        query: str, prefix: str = ""
    ) -> list[dict[str, str]]:
        _not_implemented("T12")


__all__ = [
    "JournalConfig",
    "register_journal_tools",
    "resolve_journal_config",
]

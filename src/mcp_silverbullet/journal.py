"""Journal surface — direct-FS read tools, gated by config.

T10 of the map gates the journal surface; T11 implements three of the
four tools (``journal_histogram``, ``tag_summary``,
``recent_pages``). T12 will replace the fourth
(``pages_touching_topic``). The bridge may run on a host that does
*not* have direct access to the SB space directory (e.g., a sidecar
container without a volume mount); the journal tools are an optional,
strictly-additive surface that requires
``MCP_SILVERBULLET_SPACE_PATH`` and ``MCP_SILVERBULLET_JOURNAL_TOOLS``
to enable. With either unset or the path unreadable, the bridge boots
cleanly without the journal tools and the existing ``/.fs``-backed
tools continue to work.

Two-step gate (resolved at :func:`resolve_journal_config`):

1. ``MCP_SILVERBULLET_JOURNAL_TOOLS`` is truthy — otherwise the gate
   is off and we skip every other check (operator did not opt in).
2. ``MCP_SILVERBULLET_SPACE_PATH`` is a non-empty path AND
   ``os.access(path, os.R_OK)`` — otherwise the gate is off and we
   log a one-line WARN.

The three T11 tools walk the space directory directly:

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

The fourth tool (``pages_touching_topic``) remains a placeholder until
T12 lands; it raises :exc:`ToolError` so a stray call surfaces loudly.
"""

from __future__ import annotations

import datetime as _dt
import logging
import os
import re
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


# --- registration ------------------------------------------------------


def _not_implemented(ticket: str) -> None:
    """Placeholder body for ``pages_touching_topic`` until T12 lands."""
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
    "PageRef",
    "register_journal_tools",
    "resolve_journal_config",
]

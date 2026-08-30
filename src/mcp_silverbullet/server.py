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
The bridge registers ten ``/.fs``-backed tools (``read_page`` /
``page_exists`` / ``write_page`` / ``delete_page`` /
``append_to_page`` / ``patch_page_lines`` / ``patch_page_replace``
/ ``move_page`` / ``list_pages`` / ``diff_pages``) plus one
bullet primitive (``list_tasks``) plus one resource template
(``silverbullet://page/{name}``). Each tool closes over a single
:class:`SBClient` opened at boot; SB's typed exceptions translate
to :mcp_exc:`ToolError` with the exact wording from
``docs/design.md`` § Tools § Status-code mapping, all funneled
through :func:`_translate_sb_errors`.

T10 of the v1.1 map adds an optional, gated journal surface
(``journal_histogram`` / ``tag_summary`` / ``recent_pages`` /
``pages_touching_topic``) that reads the SB space directory directly.
The gate is opt-in: ``build_mcp(..., journal=JournalConfig(enabled=True,
space_path=...))`` adds the four journal tools; otherwise the bridge
registers only the eleven ``/.fs``-backed + bullet-primitive tools
and the resource template. See :mod:`mcp_silverbullet.journal` for
the gate logic.

See ``docs/design.md`` § Tools for the tool surface, § SilverBullet
client contract for the SB-side status codes, and
``docs/wayfinder/map.md` (v1) / ``docs/wayfinder/map-v1.1.md` (v1.1)
for the T3/T4/T10/T18/T19/T20/T21 decisions this implements. The
v1.2 build map at ``docs/wayfinder/map-v1.2.md` tracks the
agent-facing QOL tickets (T23/T24/T25/T26/T27/T28/T29 done; T30 next).
"""

from __future__ import annotations

import contextlib
import difflib
from collections.abc import AsyncIterator
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
    _list_tasks_for_space,
    _parse_tasks,
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
    """
    try:
        yield
    except PageNotFound as exc:
        raise ToolError(f"page not found: {name}") from exc
    except PreconditionFailed as exc:
        raise ToolError("precondition failed; check if_match/if_none_match") from exc
    except BodyTooLarge as exc:
        raise ToolError(f"body too large: limit is {_BODY_LIMIT_MIB} MiB") from exc
    except ServerError as exc:
        raise ToolError(str(exc)) from exc
    except httpx.TimeoutException as exc:
        raise ToolError("silverbullet request timed out") from exc


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
        hydrated = await sb_client.read_page_meta_safe(meta.name)
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


def build_mcp(
    sb_client: SBClient,
    *,
    token: str,
    resource_url: str = _DEFAULT_RESOURCE_URL,
    name: str = "mcp-silverbullet",
    journal: JournalConfig | None = None,
    list_pages_hydrate_etags: bool = False,
) -> MCPServer:
    """Build the configured :class:`MCPServer`.

    Parameters
    ----------
    sb_client
        The outbound ``SBClient`` opened at boot. Held by closure; the
        server doesn't reopen it. v1 has no per-request token refresh,
        so a single client for the process lifetime is correct.
    token
        The shared bearer secret. Same value as ``MCP_SILVERBULLET_TOKEN``
        and ``SB_AUTH_TOKEN``. Compared constant-time against the
        inbound ``Authorization: Bearer …`` header.
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
        the bridge registers the four journal-surface tools in
        addition to the eleven ``/.fs``-backed + bullet-primitive
        tools (``read_page`` / ``page_exists`` / ``write_page`` /
        ``delete_page`` / ``append_to_page`` /
        ``patch_page_lines`` / ``patch_page_replace`` /
        ``move_page`` / ``list_pages`` / ``diff_pages`` /
        ``list_tasks``) and the resource template; when off, only
        the latter are exposed. Resolved by
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
        instructions=(
            "Read, write, delete, append to, patch, move, list, "
            "check existence of, diff, and enumerate checkbox "
            "tasks on SilverBullet pages. Eleven tools "
            "(`read_page`, `page_exists`, `write_page`, "
            "`delete_page`, `append_to_page`, "
            "`patch_page_lines`, `patch_page_replace`, "
            "`move_page`, `list_pages`, `diff_pages`, "
            "`list_tasks`) plus one resource template "
            "`silverbullet://page/{name}` for attaching page "
            "bodies to conversation context. The three "
            "read-modify-write tools (`append_to_page`, "
            "`patch_page_lines`, `patch_page_replace`) accept "
            "`dry_run=True` (T26) to preview the patch without "
            "committing. `list_pages` returns the full meta "
            "envelope per row (`{name, etag, size_bytes, "
            "last_modified_ms, created_ms}`); set "
            "`MCP_SILVERBULLET_LIST_PAGES_HYDRATE_ETAGS=1` to "
            "hydrate the etag from a per-page GET (T28 opt-in). "
            "`diff_pages` (T27) takes one page plus either "
            "`other_name` (a second page to diff against) or "
            "`other_body` (a literal string) and returns a "
            "line-based unified diff alongside the read-side "
            "envelopes for both pages. `list_tasks` (T29) "
            "enumerates checkbox bullets on a page "
            "(`list_tasks(page=\"name\")`) or across the whole "
            "space (`list_tasks(prefix=\"Daily\")`, requires "
            "the journal surface); the per-page form is always "
            "available via `GET /.fs/{page}`, the space-walk "
            "form requires `MCP_SILVERBULLET_JOURNAL_TOOLS=1` "
            "plus `MCP_SILVERBULLET_SPACE_PATH`."
        ),
        token_verifier=StaticTokenVerifier(token),
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
    """Attach the eleven ``/.fs``-backed + bullet-primitive tools and one resource template to ``mcp``.

    Pulled out of :func:`build_mcp` so tests can build a server and
    call the registration in isolation. ``mcp.tool()`` / ``mcp.resource()``
    are decorators that take the function; eight of the eleven tool
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
            "response headers (older SB / proxy). Returns "
            "404-equivalent ToolError if the page is missing."
        ),
    )
    async def read_page(name: str) -> dict[str, object]:
        async with _translate_sb_errors(name):
            page = await sb_client.read_page(name)
        return _read_meta_to_payload(page)

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
            return await sb_client.exists_page(name)
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
            "ToolError on precondition failure, 413 on body > 4 MiB."
        ),
    )
    async def write_page(
        name: str,
        content: str,
        if_match: str | None = None,
    ) -> dict[str, object]:
        async with _translate_sb_errors(name):
            meta = await sb_client.write_page(
                name, content, if_match=if_match
            )
        return _write_meta_to_payload(meta)

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
            "confirm what was removed. 404-equivalent ToolError "
            "if the page is missing."
        ),
    )
    async def delete_page(
        name: str,
        if_match: str | None = None,
    ) -> dict[str, object]:
        async with _translate_sb_errors(name):
            meta = await sb_client.delete_page(name, if_match=if_match)
        return _write_meta_to_payload(meta)

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
            "404s on the read."
        ),
    )
    async def append_to_page(
        name: str,
        text: str,
        if_match: str | None = None,
        dry_run: bool = False,
    ) -> dict[str, object]:
        # An empty append is almost certainly a caller bug (the
        # caller meant to write something and forgot to fill it in);
        # surface it loudly upfront so the read-modify-write round
        # trip isn't wasted on a no-op. ``write_page(name, content)``
        # is the right tool for "create with this body" and
        # ``append_to_page(name, "")`` would only ever mean that.
        if not text:
            raise ToolError("text must not be empty")
        async with _translate_sb_errors(name):
            page = await sb_client.read_page(name)
            body = page.body or ""
            new_body = (
                body + "\n" + text
                if body and not body.endswith("\n")
                else body + text
            )
            if dry_run:
                # T26: validate ``if_match`` against the read's etag
                # *here* because no PUT happens to do it on the
                # server. ``if_match="*"`` means "require existence"
                # — the read 404s on a missing page, so the helper
                # no-ops. ``if_match=None`` is unconditional. A
                # concrete-etag mismatch raises the same 412 wording
                # the live path would surface when SB returned 412,
                # so the agent sees one shape across both paths.
                _validate_if_match_on_read(page.etag, if_match)
                return _dry_run_payload(body, new_body)
            meta = await sb_client.write_page(
                name, new_body, if_match=if_match
            )
        return _write_meta_to_payload(meta)

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
            "specific ToolError the live path would surface."
        ),
    )
    async def patch_page_lines(
        name: str,
        start_line: int,
        end_line: int,
        new_content: str,
        if_match: str | None = None,
        dry_run: bool = False,
    ) -> dict[str, object]:
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
        async with _translate_sb_errors(name):
            page = await sb_client.read_page(name)
            body = page.body or ""
            lines, had_trailing_newline = _split_body_lines(body)
            line_count = len(lines)
            if end_line > line_count:
                raise ToolError(
                    f"line range {start_line}..{end_line} out of bounds "
                    f"for page with {line_count} lines"
                )
            new_body = _apply_line_patch(
                lines, start_line, end_line, new_content
            )
            # Preserve the page's trailing newline the way an editor
            # would: ``splitlines``/``join`` above drops it as a side
            # effect, so re-attach it iff the body had one and the
            # result is non-empty. An empty patched body has no
            # trailing newline either way.
            if had_trailing_newline and new_body:
                new_body += "\n"
            if dry_run:
                # T26: same ``if_match``-on-read validation as
                # ``append_to_page``. The dry-run envelope surfaces
                # the *post-shaping* ``new_body`` (with trailing
                # newline re-attached), so the diff an agent sees
                # is exactly the body that would have been written.
                _validate_if_match_on_read(page.etag, if_match)
                return _dry_run_payload(body, new_body)
            meta = await sb_client.write_page(
                name, new_body, if_match=if_match
            )
        return _write_meta_to_payload(meta)

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
    ) -> dict[str, object]:
        # Cheap, no-read input validation first. ``find == ""`` would
        # match between every character (``"abc".replace("", "X")``
        # is ``"XaXbXcX"``) — almost certainly a caller bug, not
        # the edit they wanted. Surface it loudly upfront so the
        # read-modify-write round trip isn't wasted and the bug is
        # pinned at the call site. Same pattern as
        # :func:`append_to_page`'s ``text must not be empty``.
        if not find:
            raise ToolError("find must not be empty")
        async with _translate_sb_errors(name):
            page = await sb_client.read_page(name)
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
            # without escaping: no regex, no fuzzy match, no escape
            # to forget. The ``count`` parameter threads the
            # ``replace_all`` knob (None = replace all, 1 = first
            # only — same shape as Python's ``str.replace``).
            new_body = body.replace(
                find, new_string, -1 if replace_all else 1
            )
            if dry_run:
                # T26: ``if_match`` is validated against the read's
                # etag here because no PUT happens. ``find not in
                # body`` and the multiple-match-with-default errors
                # above already raised, so by this point we know
                # the patch would have changed something — the
                # dry-run envelope surfaces the result.
                _validate_if_match_on_read(page.etag, if_match)
                return _dry_run_payload(body, new_body)
            meta = await sb_client.write_page(
                name, new_body, if_match=if_match
            )
        return _write_meta_to_payload(meta)

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
            "returns the page's full acknowledgement envelope (T23). "
            "The `if_match` precondition is not honored in this "
            "branch because there's no delete to guard and "
            "`read_page` doesn't accept a precondition — callers "
            "that need to verify the etag on a same-name no-op "
            "should chain `write_page(name, body, if_match=\"<etag>\")` "
            "themselves. Returns the new page's write "
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
            "destination write."
        ),
    )
    async def move_page(
        name: str,
        new_name: str,
        if_match: str | None = None,
    ) -> dict[str, object]:
        # Same-name short-circuit: ``name == new_name`` is a no-op
        # that returns the page's current acknowledgement without a
        # write/delete round-trip. The caller is asking us to rename
        # a page to itself — there is nothing to do, and running the
        # dance would risk spurious 412s on the source delete (we'd
        # have just written a fresh body to ``new_name`` — which is
        # also ``name`` — so the etag from the read would be stale).
        if name == new_name:
            async with _translate_sb_errors(name):
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
                page = await sb_client.read_page(name)
                return _write_meta_to_payload(page)
        async with _translate_sb_errors(name):
            # 1. Read the source body. No precondition — the source's
            # ``If-Match`` guard lives on the delete (step 3) and is
            # supplied by the *caller's* outer ``if_match`` argument,
            # not by the etag from this read (which ``read_page``
            # doesn't surface). A caller that wants the move to fail
            # 412 on a concurrent edit must thread the etag in:
            # ``read_page → move_page(name, new_name, if_match=<etag>)``.
            # A 404 here surfaces the standard
            # ``page not found: {name}`` wording.
            page = await sb_client.read_page(name)
            body = page.body or ""
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
                    new_name, body, if_none_match=True
                )
            except PreconditionFailed as exc:
                # Destination already exists — surface a clearer
                # message than the unified 412 wording. The source
                # hasn't been touched yet, so this is purely a
                # caller-side decision (pick a different new_name
                # or merge manually).
                raise ToolError(
                    f"destination page already exists: {new_name}; "
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
            await sb_client.delete_page(name, if_match=if_match)
        except PreconditionFailed as exc:
            raise ToolError(
                f"moved body to {new_name} but failed to delete "
                f"{name}: precondition failed; check if_match/if_none_match; "
                f"both now exist"
            ) from exc
        except PageNotFound as exc:
            # Edge case: ``name`` was deleted between step 1's read
            # and step 3's delete. The body is at ``new_name``,
            # which is what the caller wanted; ``name`` already
            # gone is a feature, not a bug. Surface a clear message
            # rather than the generic 404 wording.
            raise ToolError(
                f"moved body to {new_name} but {name} was already "
                f"deleted before the cleanup step"
            ) from exc
        except ServerError as exc:
            raise ToolError(
                f"moved body to {new_name} but failed to delete "
                f"{name}: {exc}; both now exist"
            ) from exc
        except httpx.TimeoutException as exc:
            raise ToolError(
                f"moved body to {new_name} but failed to delete "
                f"{name}: silverbullet request timed out; both now exist"
            ) from exc
        # Successful move: return the destination's acknowledgement.
        # ``new_meta`` already has ``name=new_name`` (write_page
        # threads the name through), so the payload's ``name`` field
        # is the destination, not the source.
        return _write_meta_to_payload(new_meta)

    @mcp.tool(
        title="List pages",
        description=(
            "List pages in the SilverBullet space, optionally filtered "
            "by prefix. v1 does the filter client-side (server-side "
            "Space Lua search is out of scope per T4 of the prior map). "
            "v1.2 T28 widened the return shape from "
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
            "`read_page` it directly."
        ),
    )
    async def list_pages(
        prefix: str = "",
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
        if prefix:
            metas = [m for m in metas if m.name.startswith(prefix)]
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
        # Read the source page inside ``_translate_sb_errors``
        # so 404 / 412 / 5xx / timeout surface as the design doc's
        # ToolError wording (matching the read tool). The second
        # read (when ``other_name`` is given) sits in its own
        # ``_translate_sb_errors`` block keyed on ``other_name``,
        # so a 404 there surfaces as ``page not found: {other_name}``
        # — the agent can tell which side is missing without
        # inspecting the call. Sequential reads, not concurrent:
        # ``difflib.unified_diff`` needs both bodies in hand, and
        # the cost is the same either way for two round trips —
        # ``asyncio.gather`` would only save wall-clock at the cost
        # of two sockets against loopback SB.
        async with _translate_sb_errors(name):
            first = await sb_client.read_page(name)
        if other_name_given:
            async with _translate_sb_errors(other_name):
                second = await sb_client.read_page(other_name)
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
                fromfile=name,
                tofile=other_name if other_name_given else "<literal>",
                lineterm="",
            )
        )
        return {
            "diff": diff,
            "name": _diff_page_envelope(first),
            "other": _diff_page_envelope(second) if other_name_given else None,
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
            # Per-page form: always available because it routes
            # through ``sb_client.read_page``, which doesn't need
            # direct FS access. The same 404 / 5xx / 412 / 413
            # / timeout wording as the read tool surfaces via
            # :func:`_translate_sb_errors`.
            async with _translate_sb_errors(page):
                result = await sb_client.read_page(page)
            body = result.body or ""
            entries = _parse_tasks(page, body)
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
        try:
            page = await sb_client.read_page(name)
        except PageNotFound as exc:
            # 404 is a ResourceNotFoundError per the SDK's two-shape
            # split: ``-32602 invalid params`` for "doesn't exist"
            # (SEP-2164), ``-32603 internal error`` for everything
            # else. ToolError would be wrong here — tools use it to
            # set ``is_error=True`` on a successful call, but
            # ``resources/read`` errors come back as JSON-RPC errors
            # and Grok's connector treats both shapes identically.
            raise ResourceNotFoundError(f"page not found: {name}") from exc
        except ServerError as exc:
            raise ResourceError(str(exc)) from exc
        except httpx.TimeoutException as exc:
            raise ResourceError("silverbullet request timed out") from exc
        return _read_meta_to_payload(page)


__all__ = [
    "build_mcp",
    "register_tools",
    "FileMeta",
    "PageMeta",
    "SBError",
    "JournalConfig",
]

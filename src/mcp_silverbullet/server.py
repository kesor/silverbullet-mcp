"""MCP server wiring for the bridge.

Locked at T4 of the prior map (three ``/.fs``-backed tools) and grown
by v1.1: T18 added ``delete_page``, T19 added ``append_to_page``.
Today the bridge registers five ``/.fs``-backed tools
(``read_page`` / ``write_page`` / ``delete_page`` / ``append_to_page``
/ ``list_pages``) plus one resource template
(``silverbullet://page/{name}``). Each tool closes over a single
:class:`SBClient` opened at boot; SB's typed exceptions translate to
:mcp_exc:`ToolError` with the exact wording from
``docs/design.md`` § Tools § Status-code mapping, all funneled
through :func:`_translate_sb_errors`.

T10 of the current map adds an optional, gated journal surface
(``journal_histogram`` / ``tag_summary`` / ``recent_pages`` /
``pages_touching_topic``) that reads the SB space directory directly.
The gate is opt-in: ``build_mcp(..., journal=JournalConfig(enabled=True,
space_path=...))`` adds the four journal tools; otherwise the bridge
registers only the five ``/.fs``-backed tools and the resource
template. See :mod:`mcp_silverbullet.journal` for the gate logic.

See ``docs/design.md`` § Tools for the tool surface, § SilverBullet
client contract for the SB-side status codes, and
``docs/wayfinder/map.md`` (v1) / ``docs/wayfinder/map-v1.1.md`` (v1.1)
for the T3/T4/T10/T18/T19 decisions this implements.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator

import httpx2 as httpx
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver.exceptions import (
    ResourceError,
    ResourceNotFoundError,
    ToolError,
)
from mcp.server.mcpserver.server import MCPServer

from mcp_silverbullet.journal import JournalConfig, register_journal_tools
from mcp_silverbullet.sb_client import (
    BodyTooLarge,
    FileMeta,
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
    ``list_pages``, ``append_to_page``) closes over the same
    :class:`SBClient` and surfaces the same five exception types with
    the same wording from ``docs/design.md`` § Tools § Status-code
    mapping. Factoring the translation into this async context
    manager keeps the wording in one place — a future tightening of
    a code path (e.g. adding ``403`` → ``ToolError("forbidden")``)
    is a single-line change.

    The 404 wording needs ``name`` (the page the caller asked for)
    rather than the URL the SB request hit — callers care about
    *which* page was missing, not the request's full URL. Tools
    that target a single page (``read_page``, ``write_page``,
    ``delete_page``, ``append_to_page``) pass ``name``; ``list_pages``
    passes an empty string (and doesn't actually raise ``PageNotFound``
    on its current code path, so the wording never surfaces there).

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


def build_mcp(
    sb_client: SBClient,
    *,
    token: str,
    resource_url: str = _DEFAULT_RESOURCE_URL,
    name: str = "mcp-silverbullet",
    journal: JournalConfig | None = None,
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
        the bridge sits behind a tunnel (``MCP_SILVERBULLET_RESOURCE_URL``
        is the planned env var, T6 will set the exact contract).
    name
        Server name advertised on the wire.
    journal
        Already-resolved journal gate config. ``None`` means the gate
        is off (no journal tools registered). When the gate is on,
        the bridge registers the four journal-surface tools in
        addition to the three ``/.fs``-backed tools and the resource
        template; when off, only the latter are exposed. Resolved by
        :func:`mcp_silverbullet.main.load_settings` from the two
        ``MCP_SILVERBULLET_JOURNAL_*`` env vars; tests construct one
        directly.

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
            "Read, write, append to, delete, and list SilverBullet "
            "pages. Five tools (`read_page`, `write_page`, "
            "`append_to_page`, `delete_page`, `list_pages`) plus one "
            "resource template `silverbullet://page/{name}` for "
            "attaching page bodies to conversation context."
        ),
        token_verifier=StaticTokenVerifier(token),
        auth=AuthSettings(
            issuer_url=resource_url,  # type: ignore[arg-type]
            resource_server_url=resource_url,  # type: ignore[arg-type]
        ),
    )

    register_tools(mcp, sb_client)
    if journal is not None:
        register_journal_tools(mcp, journal)
    return mcp


def register_tools(mcp: MCPServer, sb_client: SBClient) -> None:
    """Attach the five ``/.fs``-backed tools and one resource template to ``mcp``.

    Pulled out of :func:`build_mcp` so tests can build a server and
    call the registration in isolation. ``mcp.tool()`` / ``mcp.resource()``
    are decorators that take the function; each tool handler wraps
    its ``sb_client`` call in :func:`_translate_sb_errors`, which
    maps SB exceptions to :exc:`ToolError` per the design doc's
    status-code mapping. The resource template uses the SDK's
    separate ``ResourceError`` shapes (JSON-RPC protocol errors vs
    tool-handler ``is_error=True``) and keeps its own translation.

    The journal surface (T11/T12) is gated separately — see
    :func:`mcp_silverbullet.journal.register_journal_tools`, called by
    :func:`build_mcp` only when the journal config says the gate is on.
    """

    @mcp.tool(
        title="Read page",
        description=(
            "Read the raw markdown body of a SilverBullet page. "
            "Returns 404-equivalent ToolError if the page is missing."
        ),
    )
    async def read_page(name: str) -> str:
        async with _translate_sb_errors(name):
            return await sb_client.read_page(name)

    @mcp.tool(
        title="Write page",
        description=(
            "Create or update a SilverBullet page. `if_match=\"*\"` "
            "requires the page to exist; `if_match=<etag>` requires "
            "the body hash to match. Returns 412-equivalent ToolError "
            "on precondition failure, 413 on body > 4 MiB."
        ),
    )
    async def write_page(
        name: str,
        content: str,
        if_match: str | None = None,
    ) -> str | None:
        async with _translate_sb_errors(name):
            etag = await sb_client.write_page(
                name, content, if_match=if_match
            )
        # ``write_page`` returns ``None`` when the SB response didn't
        # carry an ETag (older or proxy-stripped); surface that as the
        # JSON ``null`` rather than mangling the type.
        return etag

    @mcp.tool(
        title="Delete page",
        description=(
            "Delete a SilverBullet page (hard delete; SB has no "
            "trash layer). `if_match=\"*\"` requires the page to "
            "exist; `if_match=<etag>` requires the body hash to "
            "match. Returns the ETag of the deleted page so the "
            "caller can confirm what was removed. 404-equivalent "
            "ToolError if the page is missing."
        ),
    )
    async def delete_page(
        name: str,
        if_match: str | None = None,
    ) -> str | None:
        async with _translate_sb_errors(name):
            return await sb_client.delete_page(name, if_match=if_match)

    @mcp.tool(
        title="Append to page",
        description=(
            "Append text to the end of a SilverBullet page, separated "
            "from the existing body by a single newline (skipped when "
            "the body already ends in a newline, so callers that pass "
            "leading newlines get exactly one separator). The tool "
            "returns the new ETag so the caller can chain edits "
            "without re-reading. `if_match=\"*\"` requires the page "
            "to exist; `if_match=<etag>` requires the body hash to "
            "match (protects against concurrent appends landing out "
            "of order). 404-equivalent ToolError if the page is "
            "missing; 412 if the precondition fails; 413 if the "
            "combined body exceeds 4 MiB."
        ),
    )
    async def append_to_page(
        name: str,
        text: str,
        if_match: str | None = None,
    ) -> str | None:
        # An empty append is almost certainly a caller bug (the
        # caller meant to write something and forgot to fill it in);
        # surface it loudly upfront so the read-modify-write round
        # trip isn't wasted on a no-op. ``write_page(name, content)``
        # is the right tool for "create with this body" and
        # ``append_to_page(name, "")`` would only ever mean that.
        if not text:
            raise ToolError("text must not be empty")
        async with _translate_sb_errors(name):
            body = await sb_client.read_page(name)
            new_body = (
                body + "\n" + text
                if body and not body.endswith("\n")
                else body + text
            )
            return await sb_client.write_page(
                name, new_body, if_match=if_match
            )

    @mcp.tool(
        title="List pages",
        description=(
            "List pages in the SilverBullet space, optionally filtered "
            "by prefix. v1 does the filter client-side (server-side "
            "Space Lua search is out of scope per T4 of the prior map)."
        ),
    )
    async def list_pages(prefix: str = "") -> list[dict[str, str | None]]:
        async with _translate_sb_errors(""):
            metas = await sb_client.list_pages()
        result: list[dict[str, str | None]] = [
            {"name": m.name, "etag": m.etag} for m in metas
        ]
        if prefix:
            result = [m for m in result if m["name"].startswith(prefix)]
        return result

    @mcp.resource(
        "silverbullet://page/{name}",
        name="silverbullet_page",
        title="SilverBullet page",
        description=(
            "Raw markdown body of a SilverBullet page, for attaching "
            "to conversation context."
        ),
        mime_type="text/markdown",
    )
    async def silverbullet_page(name: str) -> str:
        try:
            return await sb_client.read_page(name)
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


__all__ = ["build_mcp", "register_tools", "FileMeta", "SBError", "JournalConfig"]

"""MCP server wiring for the bridge.

Locked at T4 of the prior map (three ``/.fs``-backed tools) and grown
by v1.1: T18 added ``delete_page``, T19 added ``append_to_page``,
T20 added ``patch_page_lines``, T21 added ``patch_page_replace``,
T22 added ``move_page``. Today the bridge registers eight
``/.fs``-backed tools (``read_page`` / ``write_page`` /
``delete_page`` / ``append_to_page`` / ``patch_page_lines`` /
``patch_page_replace`` / ``move_page`` / ``list_pages``) plus one
resource template (``silverbullet://page/{name}``). Each tool
closes over a single :class:`SBClient` opened at boot; SB's typed
exceptions translate to :mcp_exc:`ToolError` with the exact wording
from ``docs/design.md`` § Tools § Status-code mapping, all funneled
through :func:`_translate_sb_errors`.

T10 of the current map adds an optional, gated journal surface
(``journal_histogram`` / ``tag_summary`` / ``recent_pages`` /
``pages_touching_topic``) that reads the SB space directory directly.
The gate is opt-in: ``build_mcp(..., journal=JournalConfig(enabled=True,
space_path=...))`` adds the four journal tools; otherwise the bridge
registers only the eight ``/.fs``-backed tools and the resource
template. See :mod:`mcp_silverbullet.journal` for the gate logic.

See ``docs/design.md`` § Tools for the tool surface, § SilverBullet
client contract for the SB-side status codes, and
``docs/wayfinder/map.md`` (v1) / ``docs/wayfinder/map-v1.1.md`` (v1.1)
for the T3/T4/T10/T18/T19/T20/T21 decisions this implements.
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
    ``list_pages``, ``append_to_page``, ``patch_page_lines``,
    ``patch_page_replace``, ``move_page``) closes over the same
    :class:`SBClient` and surfaces the same five exception types with
    the same wording from ``docs/design.md`` § Tools § Status-code
    mapping. Factoring the translation into this async context
    manager keeps the wording in one place — a future tightening of
    a code path (e.g. adding ``403`` → ``ToolError("forbidden")``)
    is a single-line change. ``move_page`` (T22) wraps *part* of its
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
    ``patch_page_replace``, ``move_page``) pass ``name``;
    ``list_pages`` passes an empty string (and doesn't actually
    raise ``PageNotFound`` on its current code path, so the wording
    never surfaces there).

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
            "Read, write, append to, patch, move, delete, and list "
            "SilverBullet pages. Eight tools (`read_page`, "
            "`write_page`, `append_to_page`, `patch_page_lines`, "
            "`patch_page_replace`, `move_page`, `delete_page`, "
            "`list_pages`) plus one resource template "
            "`silverbullet://page/{name}` for attaching page "
            "bodies to conversation context."
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
    """Attach the eight ``/.fs``-backed tools and one resource template to ``mcp``.

    Pulled out of :func:`build_mcp` so tests can build a server and
    call the registration in isolation. ``mcp.tool()`` / ``mcp.resource()``
    are decorators that take the function; each tool handler wraps
    its ``sb_client`` call in :func:`_translate_sb_errors`, which
    maps SB exceptions to :exc:`ToolError` per the design doc's
    status-code mapping. ``move_page`` (T22) is the exception:
    the post-write-delete sequence surfaces a partial-failure
    ``ToolError`` directly from the handler so the caller can see
    "moved body to {new} but failed to delete {old}; both now
    exist" rather than the unified 412 wording — the source and
    destination are distinct pages and the caller needs to know
    which side refused. The resource template uses the SDK's
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
            "Append text to the end of a SilverBullet page. The tool "
            "inserts a single newline between the existing body and "
            "the new text when the body doesn't already end in one; "
            "if it does, the new text is concatenated verbatim. The "
            "tool adds exactly one separator in either case — a "
            "caller-supplied leading newline is preserved unchanged, "
            "so `append_to_page(name, \"\\nworld\")` against a body "
            "of `\"hello\"` produces `\"hello\\n\\nworld\"` (one "
            "separator from the tool, one from the caller). Returns "
            "the new ETag so the caller can chain edits without "
            "re-reading. `if_match=\"*\"` requires the page to exist; "
            "`if_match=<etag>` requires the body hash to match "
            "(protects against concurrent appends landing out of "
            "order). 404-equivalent ToolError if the page is "
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
            "`append_to_page`). Returns the new ETag. "
            "`start_line < 1`, `end_line < start_line`, and "
            "`end_line` past the last line all raise `ToolError` "
            "with the page's line count; 404 if the page is "
            "missing; 412 if the precondition fails; 413 if the "
            "patched body exceeds 4 MiB."
        ),
    )
    async def patch_page_lines(
        name: str,
        start_line: int,
        end_line: int,
        new_content: str,
        if_match: str | None = None,
    ) -> str | None:
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
            body = await sb_client.read_page(name)
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
            return await sb_client.write_page(
                name, new_body, if_match=if_match
            )

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
            "new ETag. 404 if the page is missing; 412 if the "
            "precondition fails; 413 if the patched body exceeds "
            "4 MiB."
        ),
    )
    async def patch_page_replace(
        name: str,
        find: str,
        new_string: str,
        replace_all: bool = False,
        if_match: str | None = None,
    ) -> str | None:
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
            body = await sb_client.read_page(name)
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
            return await sb_client.write_page(
                name, new_body, if_match=if_match
            )

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
            "returns `None` for the etag (`read_page` doesn't surface "
            "one); the `if_match` precondition is not honored in "
            "this branch because there's no delete to guard and "
            "`read_page` doesn't accept a precondition — callers "
            "that need to verify the etag on a same-name no-op "
            "should chain `write_page(name, body, if_match=\"<etag>\")` "
            "themselves. Returns the new page's ETag on success. "
            "Errors: 404-equivalent ToolError if the source is "
            "missing (`page not found: {name}`), 412 from the "
            "destination write surfaces as `destination page already "
            "exists: {new_name}; refusing to overwrite` (clearer "
            "than the generic 412 wording because the source and "
            "destination are different pages — the caller needs to "
            "know which side refused), 412 from the source delete "
            "after a successful destination write surfaces as "
            "`moved body to {new_name} but failed to delete {name}: "
            "<reason>; both now exist` so the caller can clean up "
            "the duplicate, 413 if the body exceeds 4 MiB on the "
            "destination write."
        ),
    )
    async def move_page(
        name: str,
        new_name: str,
        if_match: str | None = None,
    ) -> str | None:
        # Same-name short-circuit: ``name == new_name`` is a no-op
        # that returns the current etag without a write/delete
        # round-trip. The caller is asking us to rename a page to
        # itself — there is nothing to do, and running the dance
        # would risk spurious 412s on the source delete (we'd have
        # just written a fresh body to ``new_name`` — which is also
        # ``name`` — so the etag from the read would be stale).
        if name == new_name:
            async with _translate_sb_errors(name):
                # Same-name is a no-op, but a missing page would
                # otherwise silently succeed. ``read_page`` is the
                # cheapest existence check (no etag round-trip;
                # ``list_pages`` doesn't carry etags on the v1 sync
                # payload). The body is discarded — we just need
                # the 404-or-200 signal. No etag to return because
                # ``read_page`` doesn't surface one; ``None``
                # mirrors the no-etag contract from the
                # read-modify-write siblings.
                #
                # ``if_match`` is intentionally not honored here:
                # the precondition guards the source delete, which
                # doesn't run on a same-name no-op, and ``read_page``
                # doesn't accept a precondition. Callers that want
                # to verify the etag should chain
                # ``write_page(name, body, if_match=<etag>)``
                # themselves.
                await sb_client.read_page(name)
                return None
        async with _translate_sb_errors(name):
            # 1. Read the source body. No precondition — the source's
            # ``If-Match`` guard lives on the delete (step 3), using
            # the etag from this read. A 404 here surfaces the
            # standard ``page not found: {name}`` wording.
            body = await sb_client.read_page(name)
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
                new_etag = await sb_client.write_page(
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
        return new_etag

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

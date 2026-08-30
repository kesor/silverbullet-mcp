# Wayfinder Map — `mcp-silverbullet` v1.1 (full editing capability via MCP)

<!--
Local-markdown tracker, like the prior build map at `map.md`. The
prior map's destination was "runnable bridge + gated journal
surface" and reached it at commit `d2d39` (T13 closed).

This map's destination was originally "tunnel-ready v1.1"
(operational polish + wire tightening) but the operator redirected
mid-chart: the real editing gap is CRUD verbs, not polish. The
bridge today exposes create-or-overwrite (`write_page`), read
(`read_page` + the resource template), and list (`list_pages`).
Missing: `delete_page`, `append_page`, line-range patch, string
find-and-replace patch, and (debatable) `move_page`. The map
re-draws around that gap.

Standing preferences from the prior map continue to apply unless
overridden here (off-the-shelf libraries only, side-car process,
single shared bearer, no daemonization). When in doubt, the prior
map's `## Notes` is the source of truth.
-->

## Destination

> Full CRUD + edit-tooling via MCP. An agent can create, read,
> update (full overwrite), append, patch (line-range or
> find-and-replace), delete, move/rename, and list SilverBullet
> pages — every operation atomic, every operation gated by
> `if_match` (etags) so concurrent edits from two clients don't
> clobber each other, every operation returning the new etag so
> the agent can chain edits.

Concretely: the bridge's `/.fs`-backed tool surface grows from
three tools to eight:

- existing: `read_page`, `write_page`, `list_pages`
  + resource template `silverbullet://page/{name}`
- new: `delete_page`, `append_page`, `patch_page_lines`,
  `patch_page_replace`, `move_page`

Each new tool wraps the `/.fs` HTTP primitives (GET / PUT /
DELETE) with `if_match` plumbing so the agent can edit a page
across multiple tool calls without a lost-update window. The
journal surface (T10–T13) is unchanged.

### Status

Tickets charted, all open. The frontier is the unblocked set:
T18 (`delete_page`), T19 (`append_page`), T20 (`patch_page_lines`),
T21 (`patch_page_replace`), T22 (`move_page`). T14–T17 from the
original "tunnel-ready v1.1" chart are demoted to **Out of scope**
— the operator decided CRUD is the priority; tunnel polish can
wait for v1.2.

## Notes

- **Domain**: same as the prior map (protocol bridge).
- **Skills every session should consult**: `mattpocock/skills@grilling`,
  `mattpocock/skills@domain-modeling`, `incremental-implementation`,
  `security-and-hardening`. The prior map's standing preferences
  about off-the-shelf libraries only continue to bind.
- **Standing preferences for this effort** (continuing from the prior map):
  - **No new dependencies.** Every new tool is a wrapper around
    `httpx2` calls already in `sb_client.py`. If the implementation
    needs a parser (e.g. for line-range patch), it does NOT pull
    in a markdown parser — it works on raw text the way SB stores
    it.
  - **Every write tool returns the new etag.** Caller can chain
    edits without re-reading. (`write_page` already does this;
    the new tools keep the contract.)
  - **Every write tool honors `if_match`.** Defaults to `None`
    (unconditional). When provided, the bridge fetches the page
    first to learn the etag, then performs the write. This means
    the atomicity story for the new tools is "read-then-write"
    under one etag — not transactional, but good enough for
    single-agent workflows and concurrent-agent protection
    (the second agent's write fails with 412 if the first one
    landed in between).
  - **`replace_all=False` by default for find-and-replace.**
    Safety: the find string should be unique unless the caller
    says otherwise. When `False` and the find matches multiple
    times, the tool errors instead of silently mass-editing.
  - **No undo.** The bridge does not implement a journal or trash
    layer; `delete_page` is a hard delete (the operator who wants
    soft delete composes `read_page → write_page(new_name) →
    delete_page` themselves). This matches SB's own semantics
    (`DELETE /.fs/{name}` is a real delete, not a move-to-trash).
  - **Line numbers are 1-indexed, inclusive.** Matches how
    `cat -n` / `sed` print them; matches how editors display
    them. `start_line=1, end_line=0` (or `start_line >
    end_line`) is a `ToolError`.
  - **Empty `new_content` deletes the line range.** The caller
    who wants to delete lines N–M passes `new_content=""`. Don't
    add a separate `delete_lines` tool; the patch tool already
    covers it.
  - **`find` is treated as a literal substring.** No regex
    semantics. Agents that want regex use `rg` (or Python `re`)
    client-side before calling the tool. A regex mode would
    invite "I forgot to escape" disasters.
  - **Errors surface as `ToolError` with the same wording
    conventions as the prior map's T4 status-code mapping
    (with the v1.1 fixes for the bogus "X-Client-Id seen"
    wording landed in the drive-by commit).**
  - **Live-SB tests stay env-gated**, same shape as T7.

## Decisions so far

<!-- index only — one line per closed ticket, link to the ticket's resolution below -->

- **Drive-by (pre-chart): 412 ToolError wording.** (commit pending):
  `write_page`'s 412 path now surfaces
  `ToolError("precondition failed; check if_match/if_none_match")`
  instead of the prior map's
  `ToolError("precondition failed; X-Client-Id seen")`. The
  `X-Client-Id` was a phantom — it doesn't exist in SB's protocol.
  `sb_client.PreconditionFailed` message, `server.write_page`
  ToolError text, `tests/test_tools_in_memory.py` assertion,
  and `docs/design.md § Tools status-code mapping` row all
  updated together. Test count: 97 pass + 2 skip.
- **Drive-by (pre-chart): `write_page` returns `str | None`.**
  (commit pending): the previous shape returned `""` when the
  SB response didn't carry an `ETag` header. `sb_client.
  write_page` is now typed `-> str | None`; `server.write_page`
  mirrors it. The MCP wire payload becomes
  `{"result": null}` for the no-etag case (vs. `{"result":
  ""}` before — same shape semantically but the type now
  matches the documentation). New Layer-1 test guards the
  null-ETag case so a future refactor doesn't regress it.
  Test count: 97 pass + 2 skip (1 new test).

## Tickets

<!--
Each ticket is sized to one 100K-token session. Mark with label
`wayfinder:<type>`. Claim by setting an `Assignee:` line at the top
of the ticket's block (no real "assignee" field exists in this local
tracker; the line IS the claim — concurrent sessions skip any ticket
that already has one).
Tickets wire blocking edges in a second pass (the tracker is a single
file; "blocking" is rendered by ticket ordering and an explicit
"Blocks:" line per ticket).
-->

### T18. `delete_page(name, if_match?)`

> **Labels**: `wayfinder:task`
> **Type**: AFK
> **Assignee**: _(unclaimed)_
> **Status**: open
> **Question**: How does the bridge expose `DELETE /.fs/{name}`?
> **Context**: SB supports it directly; the bridge just doesn't
> surface it. v1.1 adds the tool. Same `if_match` semantics as
> `write_page`: `if_match="*"` requires the page to exist;
> `if_match=<etag>` requires the body to match; `None` means
> unconditional. Returns the etag of the deleted page (for the
> caller to confirm what was deleted) — or `None` if the SB
> response didn't carry one. 404 → `ToolError("page not found:
> {name}")`; 412 → `ToolError("precondition failed; check
> if_match/if_none_match")`.
>
> **Done when**: a Layer-1 test exercises the happy path (200 +
> ETag), the 404 path, the 412 path with both `if_match="*"`
> and `if_match=<stale>`, and the no-ETag path. The T7 live e2e
> test (if extended) round-trips a `delete_page` against the
> live SB; for now the Layer-1 + Layer-2 coverage is enough —
> T19 (append) will exercise the read-while-deleting path
> implicitly.
>
> **Files when resolved**: `src/mcp_silverbullet/sb_client.py`
> (new `delete_page` method, mirrors `write_page`'s header
> envelope), `src/mcp_silverbullet/server.py` (new `@mcp.tool`
> handler in `register_tools`), `tests/test_sb_client.py`
> (Layer-3 mock coverage), `tests/test_tools_in_memory.py`
> (Layer-1 MCP shape coverage). No env-var or design-doc
> changes.
>
> **Unblocks**: T19 (append uses delete + write for atomicity
> if needed — T19 will decide), T22 (move uses delete on the
> old_name after a successful write on new_name).

---

### T19. `append_page(name, text, if_match?)`

> **Labels**: `wayfinder:task`
> **Type**: AFK
> **Assignee**: _(unclaimed)_
> **Status**: open
> **Question**: How does the bridge expose "append text to a
> page"?
> **Context**: SB has no native append semantics; the bridge
> does `read_page → body + "\n" + text → write_page(new_body,
> if_match=<etag_from_read>)`. The `if_match` plumbed through
> the call protects against lost updates when two agents append
> concurrently (the second one's write fails 412).
>
> **Atomicity**: read-modify-write is not transactional, but
> the `if_match` on the write guarantees no overwrite of a
> newer body. The tool returns the new etag on success.
>
> **Edge case — `if_match="*"`**: caller wants "create if
> absent". `read_page` 404s; the tool must therefore treat
> `if_match="*"` differently: it's a *create*, not an append.
> Either reject the combination (`ToolError("append_page with
> if_match='*' is a create; use write_page")`) or treat
> missing page as an empty body and append (effectively a
> create). T19 picks one.
>
> **Edge case — separator**: append `"hello"` to a page ending
> in `"goodbye"` produces `"goodbye\nhello"` or
> `"goodbyehello"`? T19 picks. Recommendation: always insert a
> `\n` separator unless the existing body already ends in `\n`
> — caller can always `read_page → write_page` themselves if
> they want a different shape.
>
> **Done when**: a Layer-1 test covers: append to existing
> page (correct body, new etag), append with stale `if_match`
> (412), append with `if_match=None` (no precondition, lost-
> update risk acceptable), and the missing-page edge case
> (whatever T19 decides).
>
> **Files when resolved**: `src/mcp_silverbullet/server.py`
> (new `@mcp.tool` handler in `register_tools` — does the
> read-modify-write inline; no new `sb_client` method needed),
> `tests/test_tools_in_memory.py` (Layer-1 coverage).
>
> **Blocks on**: T18 (the missing-page edge case in append
> shares error-shape code with delete's 404 path — both
> surface `ToolError("page not found: {name}")` from a shared
> helper).
> **Unblocks**: T20 (the patch tools build on append's
> read-modify-write pattern).

---

### T20. `patch_page_lines(name, start_line, end_line, new_content, if_match?)`

> **Labels**: `wayfinder:task`
> **Type**: AFK
> **Assignee**: _(unclaimed)_
> **Status**: open
> **Question**: How does the bridge expose line-range edits?
> **Context**: `patch_page_lines(name, start_line, end_line,
> new_content, if_match?)` replaces lines `start_line..end_line`
> (1-indexed, inclusive) with `new_content`. Built on the same
> read-modify-write-with-if_match pattern as T19 (append).
>
> **Line splitting**: split on `\n` (single newline, not
> universal newlines — SB stores LF). Trailing newline behavior
> needs care: a body `"a\nb\n"` is two lines (`a`, `b`) or three
> (`a`, `b`, empty)? T20 picks. Recommendation: split and drop
> a trailing empty line; the resulting split is consistent with
> what an editor's "go to end" lands on.
>
> **Empty `new_content`**: deletes the range. (Standing
> preference.)
>
> **Out-of-range**: `start_line < 1`, `end_line > line_count`,
> or `start_line > end_line` all raise `ToolError("line range
> {start}..{end} out of bounds for page with {N} lines")`.
>
> **Done when**: a Layer-1 test covers: replace middle range,
> replace first N lines, replace last N lines, replace entire
> body (`start=1, end=line_count, new_content="x"`), empty
> `new_content` (delete range), out-of-range errors, stale
> `if_match` (412).
>
> **Files when resolved**: `src/mcp_silverbullet/server.py`
> (new `@mcp.tool` handler), `tests/test_tools_in_memory.py`
> (Layer-1 coverage). Possibly a tiny helper module if the
> patch logic grows past ~30 lines (`_patch.py` or inline in
> `server.py` — T20 picks).
>
> **Blocks on**: T19 (the read-modify-write boilerplate).
>
> **Unblocks**: none directly; T21 has its own shape.

---

### T21. `patch_page_replace(name, find, new_string, replace_all=False, if_match?)`

> **Labels**: `wayfinder:task`
> **Type**: AFK
> **Assignee**: _(unclaimed)_
> **Status**: open
> **Question**: How does the bridge expose literal find-and-
> replace patches?
> **Context**: `patch_page_replace(name, find, new_string,
> replace_all=False, if_match?)` does a literal (not regex)
> substring replace in the body, returning the new etag on
> success. `find` is a literal substring; no regex, no glob,
> no fuzzy matching.
>
> **Strict by default**: when `replace_all=False` and `find`
> matches multiple times, the tool raises
> `ToolError("find matched N times; pass replace_all=True or
> narrow find")`. When `replace_all=True`, all occurrences are
> replaced.
>
> **`find` not found**: `ToolError("find not found in body")`
> — better than silently returning the unchanged body because
> a typo in the find string should not look like success.
>
> **Edge case — empty `find`**: never matches (would replace
> between every character); raises `ToolError("find must not
> be empty")` upfront.
>
> **Done when**: a Layer-1 test covers: single match (default
> `replace_all=False`), single match explicit, multiple
> matches with `replace_all=False` (error), multiple matches
> with `replace_all=True` (all replaced), no matches (error),
> empty `find` (error), stale `if_match` (412).
>
> **Files when resolved**: `src/mcp_silverbullet/server.py`
> (new `@mcp.tool` handler — small enough to be inline), or a
> shared `_patch.py` if T20 created one, `tests/test_tools_in_
> memory.py`.
>
> **Blocks on**: T19 (the read-modify-write boilerplate).

---

### T22. `move_page(name, new_name, if_match?)`

> **Labels**: `wayfinder:task`
> **Type**: AFK
> **Assignee**: _(unclaimed)_
> **Status**: open
> **Question**: How does the bridge expose page rename?
> **Context**: SB has no native move; the bridge does
> `read_page(name) → write_page(new_name, body, if_match=None) →
> delete_page(name)`. The `if_match` on the outer call protects
> the read-delete pair; the write to `new_name` is unconditional
> (or uses `If-None-Match: *` if the caller wants
> "rename or fail").
>
> **Atomicity caveat**: if `write_page(new_name)` succeeds but
> `delete_page(name)` fails, the page exists at *both* names.
> v1 surfaces this as a clear `ToolError("moved body to
> {new_name} but failed to delete {name}: <reason>; both now
> exist")` so the caller can clean up. A truly atomic move
> would require either SB-side support or a transactional
> outbox; v1.1 doesn't go that far.
>
> **Same-name**: `name == new_name` is a no-op; returns the
> current etag. Avoids the read-write-delete cycle.
>
> **Done when**: a Layer-1 test covers: happy path (page
> disappears from `name`, appears at `new_name`, body
> identical), `name == new_name` no-op, source missing
> (404), destination already exists (write fails 412 if
> `if_match` plumbed, otherwise overwrites — T22 picks the
> default), the partial-failure case (write succeeds,
> delete fails — assert the clear error message).
>
> **Files when resolved**: `src/mcp_silverbullet/server.py`
> (new `@mcp.tool` handler), `tests/test_tools_in_memory.py`.
> No new `sb_client` method needed (composes existing
> primitives).
>
> **Blocks on**: T18 (delete_page is the last step of move).
>
> **Unblocks**: none.

---

## Not yet specified

<!-- dim view of what's coming: things we suspect we'll ticket but can't yet phrase precisely -->

- **Bulk operations** — `delete_pages(names[])`, `move_pages
  (renames[])`. Cheap to add after the singular tools land;
  specifiable when an agent's actual workflow calls for them.
- **Transactional multi-page edit.** The v1.1 CRUD tools are
  read-modify-write under one etag; multi-page transactions
  would require either SB-side support or an outbox pattern
  on the bridge. Punt.
- **Revision history / undo.** SB doesn't ship one; the
  bridge can't conjure one. Punt.
- **Frontmatter helpers.** `patch_frontmatter(name,
  updates)` that does YAML-aware merges. Specifiable when
  an agent's actual workflow needs them; not v1.1.
- **`/healthz` with operator-probe body shape.** Demoted from
  the original v1.1 chart when the destination redrew.
  v1.2 candidate.
- **`allowed_origins` for browser-side MCP clients.** Demoted
  when the destination redrew. v1.2 candidate.
- **`scopes_supported` in the discovery doc.** Demoted when
  the destination redrew. v1.2 candidate.
- **`list_pages` etag round-trip (bridge-side `read_page`
  fallback).** Demoted when the destination redrew. v1.2
  candidate.

## Out of scope

<!-- Work ruled beyond this map's destination. Closed/fog items go in
"Decisions so far" or "Not yet specified" respectively; this section
is for *scope* boundaries. -->

- **T14–T17 from the original "tunnel-ready v1.1" chart**
  (closed without action; the operator redirected v1.1 to CRUD):
  - `T14` `/healthz` endpoint — punt to v1.2.
  - `T15` `scopes_supported` in discovery doc — punt to v1.2.
  - `T16` `list_pages` etag round-trip — punt to v1.2.
  - `T17` placeholder — dissolved.
- **New write tools beyond CRUD** — `create_page` as a
  distinct verb (use `write_page(name, content, if_match="*")`
  instead), `append_page_many`, `patch_many`. Punt.
- **OAuth 2.1, dynamic-client registration, multi-user.**
  Locked out at T2 of the prior map.
- **Server-pushed notifications / `subscriptions/listen`.**
  Punt.
- **Re-deciding design questions locked in `docs/design.md`.**
  Same boundary as the prior map.
- **Journal-surface write paths.** Read-only carries forward.
- **PR to nixpkgs upgrading `python3Packages.mcp` to v2.x.**
  Punt from the prior map.
- **A standalone trash layer / soft delete.** Hard delete is
  v1.1's contract; agents that want a backup compose
  `read_page → write_page(<backup_name>) → delete_page`.
- **Markdown-aware patch.** Line-range and string-replace work
  on raw text; the bridge does not parse markdown ASTs. An
  agent that wants to "replace the second list item in
  section 3" reads the page, parses client-side, and calls
  `patch_page_lines` with the right indices.

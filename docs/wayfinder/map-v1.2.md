# Wayfinder Map — `mcp-silverbullet` v1.2 (agent-facing QOL + bullet primitives)

<!--
Local-markdown tracker, like the prior build map (`map.md`, v1) and
the v1.1 map (`map-v1.1.md`, full CRUD + editing). Both prior maps
are at destination reached.

Standing preferences from the prior maps continue to apply unless
overridden here:

- Off-the-shelf libraries only — `mcp==2.1.1`, `httpx2`, Starlette,
  uvicorn, pytest. No new Python deps for v1.2 unless a ticket
  explicitly says otherwise.
- Side-car process; one bearer secret on both hops; no daemonization;
  no OAuth 2.1 dance.
- Every write tool honors `if_match`.
- Layer 1 (in-memory `Client(mcp)`), Layer 2 (real ASGI transport),
  Layer 3 (`httpx.MockTransport` against `sb_client`) test split from
  the v1 map's T3 / T4 / T5 carry-forwards. Live e2e stays env-gated
  per the v1 T7 shape.

When in doubt, `docs/wayfinder/map.md` and
`docs/wayfinder/map-v1.1.md` are the source of truth on the standing
preferences; this map inherits them.
-->

## Destination

> **v1.2: agent-facing quality-of-life + bullet/checklist primitives.**
> Eight tickets. Every read and every write tool returns enough
> metadata that the agent doesn't need a follow-up read to verify
> what just happened; the bridge gains lightweight helpers
> (`page_exists`, `dry_run`, `diff_pages`, `list_pages` etag
> round-trip) that close the most common "I just edited, now I need
> to verify" round-trips; and the bridge gains the bullet primitives
> (`check_task`, `list_tasks`) that let an agent actually mark
> tasks done without composing read-modify-write against
> `patch_page_lines`.

The shape:

- **Acknowledge-shape pair (T23, T24)**: every write tool returns
  `{name, etag, size_bytes, last_modified_ms, created_ms}`. Read
  tools return `{body, etag, size_bytes, last_modified_ms}`. These
  two land together because the wire-shape decision is the same and
  the test surface overlaps heavily.
- **Lightweight helpers (T25, T26, T27)**: `page_exists(name) -> bool`,
  `dry_run=True` mode on the patch tools, `diff_pages(name,
  other_name?)` / vs a literal string.
- **List-pages metadata (T28)**: `list_pages` returns the full
  metadata shape (matching T23/T24) AND the bridge falls back to
  per-name `read_page` to hydrate etags when SB's `/.fs` list
  payload omits them.
- **Bullet primitives (T29, T30)**: `list_tasks(page?, prefix?)` and
  `check_task(page, ref, state="done")`. The ref is the wikilink
  text on the same bullet (the convention SB's editor uses
  internally — see `externalTaskRef` in the SB client); the tool
  finds the bullet by that ref, flips `[ ]` / `[x]` / `[X]`, and
  returns the new ETag.

The destination is reached when all eight tickets are closed.

## Notes

- **Domain**: same as the prior maps (protocol bridge). New v1.2
  surface stays inside the existing MCP-SB boundary — no new
  transports, no new auth hop.
- **Skills every session should consult**: `mattpocock/skills@grilling`,
  `mattpocock/skills@domain-modeling`, `incremental-implementation`,
  `security-and-hardening`. The prior maps' standing preferences
  (off-the-shelf libraries only, side-car process, single shared
  bearer, no daemonization) continue to bind.
- **Standing preferences for this effort**:
  - **No new Python dependencies** unless a ticket says otherwise.
    The bullet work needs to find a wikilink inside a body string;
    the prior maps' `pages_touching_topic` (T12) already does
    substring matching with `str.find` / `re.search`; reuse those
    helpers, no new deps.
  - **Breaking wire-shape changes are *not* avoided.** v1.2
    changes the *return type* of every read/write tool (T23/T24
    widen to a meta envelope; T28 widens list_pages to the same
    envelope). The MCP wire payload for the existing string /
    list-of-dict returns grows an outer dict; tests have to be
    updated to match. MCP clients that consume the old shape will
    break — note this loudly in the README + CHANGELOG when T23
    lands. The new shape is the right call (the user's framing:
    "indication regarding their success with enough content and
    information so that the agent wouldn't need to guess") but
    it's a breaking change for any client pinned to the v1.1 wire
    shape.
  - **`dry_run` is pure.** T26 must NOT call `sb_client.write_page`
    on the dry-run path. The whole point is "show me what would
    happen"; a dry run that mutates is a bug.
  - **`page_exists` is a HEAD-equivalent.** T25 may issue
    `GET /.fs/{name}` and surface 200 vs 404 — that's what the
    HTTP semantics call for. Don't over-engineer with a separate
    HEAD request; SB may not honor HEAD the same way.
  - **`diff_pages` is line-based by default.** T27 ships a unified
    diff (or `difflib`-shaped diff) — agent picks two pages
    (or a page and a literal string) and gets the diff back. No
    word-level or token-level diffing in v1.2; that's a v1.3
    refinement.
  - **Bullet ref = wikilink text on the same line.** T29/T30 use
    the same convention SB's editor uses internally: a task is
    addressable iff the bullet line contains a wikilink (`[[…]]`)
    that resolves to a `position` / `linecolumn` / `anchor` target
    (per the SB client-side `case "Task"` rendering rule).
    Bullets without a wikilink ref are *not* addressable by these
    tools — the agent falls back to `patch_page_lines` as today.
    Auto-migrating bullets to add a synthetic wikilink is
    explicitly out of scope: destructive, the user didn't ask for
    it, and it would change the meaning of existing pages.
  - **Live-SB tests stay env-gated**, same shape as the v1 T7 and
    v1.1 T19 / T21 / T22 carry-forwards.

## Decisions so far

<!-- index only — one line per closed ticket, link to the ticket's
resolution below -->

- **T23. Write-tool acknowledgement shape.** (commit pending):
  Every write tool (`write_page` / `delete_page` / `append_to_page` /
  `patch_page_lines` / `patch_page_replace` / `move_page`) now
  returns `{name, etag, size_bytes, last_modified_ms, created_ms}`
  instead of `str | None`. The change rides on a new `PageMeta`
  dataclass in `sb_client.py` (single-source-of-truth envelope)
  that all three client entry points (`read_page` / `write_page` /
  `delete_page`) now return; the MCP tool layer subsets the envelope
  to the T23 wire shape via `_write_meta_to_payload` in `server.py`.
  `read_page` (the MCP tool) keeps returning `str` for v1.2-rc1;
  T24 widens it to `{body, etag, size_bytes, last_modified_ms}`
  with the same one-line unwrap that the resource template already
  uses. `list_pages` keeps its v1.1 `list[{name, etag}]` shape; T28
  widens it. The client-side `list_pages` stays returning `FileMeta`
  until T28, then both widen together.

  **Field-by-field contract**: `name` is the page the caller asked
  about; `etag` is `None` when SB stripped the response header;
  `size_bytes` is the UTF-8 byte count of the just-written body
  (always populated on writes — independent of whether SB echoes
  `X-Content-Length` back, so a stripped response still surfaces a
  real number); `last_modified_ms` and `created_ms` come from
  `X-Last-Modified` / `X-Created` and are `None` when SB stripped
  them. `delete_page` returns `size_bytes=None` and both
  timestamps as `None` because SB's DELETE response doesn't carry
  the `X-*` headers per the design doc § SilverBullet client
  contract DELETE row — honest wire shape, no fabricated numbers.
  `move_page` returns the **destination's** envelope on success;
  the same-name no-op (`name == new_name`) returns the source's
  envelope (the read on the existence check surfaces full meta
  since the client side was widened in the same change).

  **Breaking change, loudly called out**: the README and a new
  `CHANGELOG.md` flag the wire-shape change with a one-line
  migration note (`result.text` → `result["result"]["etag"]`,
  plus the new meta fields so the agent can skip the v1.1
  follow-up read). `docs/design.md` § Tools table got a "Returns"
  column reflecting the new shape on every write tool (plus a
  pointer to T24 / T28 for the read-side / list widening).

  **Files touched**: `src/mcp_silverbullet/sb_client.py` (added
  `PageMeta` dataclass + `_meta_from_response` + `_parse_int_header`
  helpers; widened `read_page` / `write_page` / `delete_page`
  return types), `src/mcp_silverbullet/server.py` (added
  `_write_meta_to_payload` projection; widened every write tool's
  return type from `str | None` to `dict[str, object]`; updated
  tool descriptions to call out the new shape; resource template
  unwraps `page.body` for now, ready for T24's one-line widening),
  `tests/test_sb_client.py` (+4 tests: `read_page` returns
  PageMeta; `read_page` extracts X-* meta from response headers;
  `read_page` tolerates malformed X-* headers;
  `write_page` size_bytes from request body; write_page meta is
  None when response stripped), `tests/test_tools_in_memory.py`
  (12 happy-path / None-ETG tests updated to assert on the new
  envelope; `move_page` same-name no-op test updated to assert
  on the source envelope), `tests/test_e2e_live_sb.py` (every
  write call now also asserts on `size_bytes` and the right
  `name` field — destination for `move_page`, source for the
  same-name no-op), `README.md` (new "v1.2 wire-shape changes"
  section under "What it exposes" with the migration snippet),
  `CHANGELOG.md` (new file, Keep-a-Changelog format, v1.2
  breaking-change called out at the top), `docs/design.md` §
  Tools table (added "Returns" column).

  **Bonus improvements visible while doing it**: the
  `_translate_sb_errors` docstring's prior v1.1 contract
  (PageMeta as `str | None`) is gone; the move-page same-name
  no-op now returns real meta (it used to return `None` because
  `read_page` didn't surface an etag); the read-modify-write
  tools (`append_to_page` / `patch_page_lines` /
  `patch_page_replace`) now thread the *write's* meta back, not
  the read's, so the etag / size / timestamps reflect what was
  actually written rather than what was read.

  **Unblocks**: T24 (read tool widens to the same envelope —
  client already returns PageMeta), T28 (list_pages widens to
  `list[PageMeta]` — dataclass already exists), T30 (check_task
  returns the same T23 ack envelope, so client change is shared
  with `move_page`).

  Test count: 184 pass + 2 skip (was 180 pass + 2 skip; +4 new
  sb_client tests + net +0 in-memory tests, since the existing
  13 wire-shape tests were rewritten in place to assert on the
  new envelope rather than split into more tests). `nix flake
  check` green.

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

### T23. Write-tool acknowledgement shape ✅

> **Labels**: `wayfinder:task`
> **Type**: AFK
> **Status**: ✅ closed (see Decisions so far)
> **Question**: What does every write tool return?
>
> **Context**: Today every write tool returns `str | None` (the new
> ETag). An agent that gets back `"68b86dbf23c1e"` has no idea how
> big the body is now, when it was last modified, or whether the
> write was a create vs an update. v1.2 changes the return shape to
> a dict: `{name, etag, size_bytes, last_modified_ms, created_ms}`.
> The seven write tools (`write_page`, `delete_page`, `append_to_page`,
> `patch_page_lines`, `patch_page_replace`, `move_page`,
> `journal_*` if any) all return the same shape. `delete_page`'s
> `created_ms` / `last_modified_ms` reflect the *deleted* page's
> timestamps (echoed from SB's response, same as the ETag echo).
> `move_page` returns the *destination* page's metadata.
>
> **Done when**: every write tool's return type is the new shape,
> the Layer-1 in-memory tests assert on the new shape, the live
> e2e tests pass with the new shape, and the v1.1 Layer-3 tests
> that assert on `str | None` are updated.
>
> **Breaks clients**: yes. The README and (if it exists) CHANGELOG
> must call out the wire-shape change. This is the breaking v1.2
> change; everything else is additive.
>
> **Files when resolved**: `src/mcp_silverbullet/server.py`
> (write-tool handlers), `src/mcp_silverbullet/sb_client.py`
> (extract metadata from response headers if SB carries them;
> otherwise `last_modified_ms` / `created` are best-effort from
> the response headers and may be `None`). Tests in
> `tests/test_tools_in_memory.py`, `tests/test_sb_client.py`,
> `tests/test_journal_gate.py`, `tests/test_journal_read.py`,
> `tests/test_journal_search.py`, `tests/test_e2e_live_sb.py`.
>
> **Blocks on**: T1 / T4 / T18 / T19 / T20 / T21 / T22 of the prior
> maps. **Unblocks**: T24 (read returns the same fields), T28
> (list_pages matches).

---

### T24. Read-tool acknowledgement shape

> **Labels**: `wayfinder:task`
> **Type**: AFK
> **Status**: ⬜ open
> **Question**: What does `read_page` return?
>
> **Context**: Today `read_page` returns `str` (just the body). v1.2
> changes it to `{body, etag, size_bytes, last_modified_ms}` —
> same metadata fields as T23's write tools, with `body` carrying
> the markdown string. The `silverbullet://page/{name}` resource
> template gets the same shape (it's just a `read_page` wrapper
> that calls `sb_client.read_page` and surfaces the body — the
> resource handler returns the dict and the SDK's MIME type
> machinery picks `text/markdown` from the body or falls back to
> the resource's registered MIME type).
>
> **Done when**: `read_page` and `silverbullet://page/{name}` return
> the new shape; Layer-1 + Layer-2 + T7 live tests assert on it;
> the v1.1 Layer-3 tests that assert on the bare string are updated.
>
> **Breaks clients**: yes. Same wire-shape break as T23.
>
> **Files when resolved**: `src/mcp_silverbullet/server.py` (read
> handler + resource handler), `src/mcp_silverbullet/sb_client.py`
> (`read_page` returns the metadata alongside the body).
>
> **Blocks on**: T23 (so the new dict shape is one consistent
> design). **Unblocks**: T28 (list_pages matches).

---

### T25. `page_exists(name) -> bool`

> **Labels**: `wayfinder:task`
> **Type**: AFK
> **Status**: ⬜ open
> **Question**: How does the bridge expose a cheap existence check?
>
> **Context**: Today, "does `Areas/Foo.md` exist?" costs a full
> `read_page` round trip. `page_exists(name) -> bool` issues
> `GET /.fs/{name}`, translates 200 → `True`, 404 → `False`, and
> 5xx → `ToolError` (the caller cares about a definitive answer;
> a 5xx is an actual problem, not "doesn't exist"). The body is
> discarded. The tool does NOT return the ETag; that's `read_page`'s
> job. If the agent needs to know "exists AND has etag X",
> `read_page` is one round trip away.
>
> **Done when**: a Layer-1 test covers 200 → True, 404 → False,
> 5xx → ToolError, and the body is discarded.
>
> **Files when resolved**: `src/mcp_silverbullet/sb_client.py`
> (new `head_page` or `exists` method that issues GET and
> discards the body), `src/mcp_silverbullet/server.py`
> (new `@mcp.tool` handler), `tests/test_tools_in_memory.py`
> (Layer-1 coverage).
>
> **Blocks on**: T4 (the tool registration machinery). **Unblocks**:
> none directly.

---

### T26. `dry_run=True` on the patch tools

> **Labels**: `wayfinder:task`
> **Type**: AFK
> **Status**: ⬜ open
> **Question**: How do the patch tools expose a preview mode?
>
> **Context**: An agent that wants to verify "this `patch_page_replace`
> would actually find the substring and replace it" today has to
> either trust the tool (and roll back if it didn't match) or read
> the page client-side, simulate the patch, and call the tool.
> `dry_run=True` on `append_to_page`, `patch_page_lines`, and
> `patch_page_replace` runs the in-memory patch and returns the
> patched body (and the diff against the original) **without**
> calling `sb_client.write_page`. The tool errors on the same
> preconditions (`if_match` is honored, "find not in body" is
> raised, etc.) — same surface as the live path, minus the write.
>
> **Wire shape**: the dry-run tool returns
> `{dry_run: True, original: str, patched: str, diff: str}` (diff
> is a unified diff from `difflib.unified_diff` for now). The
> caller can decide what to do with it.
>
> **Done when**: a Layer-1 test for each of the three tools covers
> dry-run success, dry-run finding the same preconditions the live
> path would (`if_match="<stale>"` → 412-equivalent ToolError even
> though the read still happened; "find not in body" → ToolError),
> and the assertion that **no write was issued** (track
> `sb_client.write_page` calls in the test mock).
>
> **Files when resolved**: `src/mcp_silverbullet/server.py` (the
> three patch handlers get a `dry_run: bool = False` parameter),
> `tests/test_tools_in_memory.py`.
>
> **Blocks on**: T19 / T20 / T21 of the v1.1 map. **Unblocks**:
> none.

---

### T27. `diff_pages(name, other_name?, other_body?)`

> **Labels**: `wayfinder:task`
> **Type**: AFK
> **Status**: ⬜ open
> **Question**: How does the bridge expose a diff between two pages
> (or a page and a literal string)?
>
> **Context**: Today an agent that just patched something and wants
> to verify the change has to read both pages client-side and diff
> them with `difflib` themselves. `diff_pages(name, other_name?)` —
> given exactly one of `other_name` (a page to diff against) or
> `other_body` (a literal string to diff against) — fetches the
> first page (and the second page if `other_name` is given), runs
> `difflib.unified_diff`, and returns the unified diff string
> alongside `{name, body, etag, size_bytes, last_modified_ms}` for
> the first page (matching T24's shape) and `{name, body, etag, …}`
> for the second page if `other_name` was given.
>
> **Errors**: passing neither `other_name` nor `other_body` →
> `ToolError("pass exactly one of other_name or other_body")`.
> Passing both → same error. The page-not-found case on either side
> → standard `ToolError("page not found: {name}")`.
>
> **Done when**: Layer-1 tests cover: page vs page (both exist),
> page vs literal string, neither given, both given, source page
> missing, comparison page missing. Layer-3 coverage for the
> `sb_client` shape.
>
> **Files when resolved**: `src/mcp_silverbullet/server.py`
> (new `@mcp.tool` handler), `tests/test_tools_in_memory.py`.
>
> **Blocks on**: T24 (so the page-shape return is consistent).
> **Unblocks**: none.

---

### T28. `list_pages` metadata shape + etag round-trip fallback

> **Labels**: `wayfinder:task`
> **Type**: AFK
> **Status**: ⬜ open
> **Question**: What does `list_pages` return, and how does the
> bridge hydrate etags when SB's `/.fs` list payload omits them?
>
> **Context**: Today `list_pages` returns `[{name, etag}]`. v1.2
> changes it to `[{name, etag, size_bytes, last_modified_ms,
> created_ms}]` — matching T23/T24's shape, minus the body. SB's
> `/.fs` list payload (when called with `X-Sync-Mode: 1`) carries
> `name` / `created` / `lastModified` / `contentType` / `size` /
> `perm` per `server/src/handlers/fs.rs::handle_fs_list`, but does
> NOT carry an `etag` field on this SB build — the v1 map's T10
> decision documented this. The bridge-side fallback: when the
> `etag` field is absent, the bridge issues a per-name HEAD
> (or cheap GET) to fetch the etag. This is an N+1 against the
> SB API; for the operator's ~200-page space it's a 200-round-trip
> cost — fine for v1.2, possibly worth a bulk HEAD endpoint in v1.3
> if measured need arises.
>
> The fallback is opt-in via an env var
> `MCP_SILVERBULLET_LIST_PAGES_HYDRATE_ETAGS` (default off — the
> v1 behavior). When off, `list_pages` returns `etag=None` per
> entry, same as today. When on, every list call pays the N+1
> cost. The operator who needs the etag round-trip turns it on;
> the operator who wants a fast listing leaves it off and uses
> `read_page` to hydrate the etag for specific pages.
>
> **Done when**: Layer-1 tests cover the off mode (etags are
> `None`, no per-name round trips), the on mode (etags are
> hydrated), the partial-hydration case (some pages have etags
> in the list payload, some don't), and the env-var parsing.
> Layer-3 test covers `sb_client`'s `_iter_md`-style hydration
> helper. Live e2e (T7-shaped) covers the on-mode behavior against
> `/var/lib/silverbullet`.
>
> **Files when resolved**: `src/mcp_silverbullet/sb_client.py`
> (new `_hydrate_etags` helper, opt-in via a constructor flag or
> per-call option), `src/mcp_silverbullet/main.py` (env var),
> `src/mcp_silverbullet/server.py` (`list_pages` shape change).
>
> **Blocks on**: T23, T24. **Unblocks**: none.

---

### T29. `list_tasks(page?, prefix?) -> [{ref, line, state, text}]`

> **Labels**: `wayfinder:task`
> **Type**: AFK
> **Status**: ⬜ open
> **Question**: How does the bridge enumerate checkboxes?
>
> **Context**: A SilverBullet "task" is a markdown bullet with a
> `- [ ]` / `- [x]` / `- [X]` checkbox marker. SB's editor uses
> the wikilink on the same bullet (if any, resolved as
> `position` / `linecolumn` / `anchor`) as the task's external
> ref. `list_tasks` walks every checkbox bullet on a page (or
> the whole space when `page` is omitted, filtered by `prefix`
> on the file name) and returns one entry per bullet:
> `{ref, line, state, text}` where:
>
> - `ref` is the wikilink text on the bullet, or `None` if the
>   bullet has no wikilink (in which case the task is not
>   addressable by T30's `check_task`; the agent falls back to
>   `patch_page_lines`).
> - `line` is the 1-indexed line number of the bullet on the page.
> - `state` is one of `" "` (todo), `"x"` (done), `"X"` (done,
>   cancelled — SB's third state).
> - `text` is the rest of the bullet line after the `[ ]` marker
>   (or the full bullet for the journal-style lines that have the
>   marker at the start).
>
> Both `page` and `prefix` are optional; the tool walks the space
> directory when `page` is omitted, matching T11's
> `journal_histogram` / `tag_summary` / T12's
> `pages_touching_topic` shape (same `_iter_md` walker, same
> prefix validation, same hidden-dir skip).
>
> **Done when**: Layer-1 tests cover: page with mix of addressable
> (wikilink) and non-addressable bullets, page with no bullets,
> missing page → `ToolError("page not found: {name}")`, prefix
> filtering, hidden-dir skip, the three states (`" "`, `"x"`,
> `"X"`). Live e2e (T13-shaped) covers
> `/var/lib/silverbullet/Areas/Kanban/Kanban Board - Hobbies.md`
> or equivalent.
>
> **Files when resolved**: `src/mcp_silverbullet/journal.py`
> (new `_list_tasks(space_root, page, prefix)` walker; the
> space-walk variant is gated behind the journal-tools env vars
> from T10 the same way the other journal tools are, while the
> per-page variant is always available because the bridge can
> `read_page` any page it has access to).
>
> **Blocks on**: T10 (journal gate), T11/T12 (the walker
> pattern). **Unblocks**: T30.

---

### T30. `check_task(page, ref, state="done") -> {page, name, etag, ...}`

> **Labels**: `wayfinder:task`
> **Type**: AFK
> **Status**: ⬜ open
> **Question**: How does the bridge flip a checkbox by ref?
>
> **Context**: SB's editor calls `index.updateTaskState(ref, ...)`
> internally to flip a task's state. That syscall is not exposed
> over the HTTP `/.fs` API. The bridge's `check_task` composes
> the same effect: `read_page(page)` → find the bullet that
> contains `ref` as a wikilink → flip the `[ ]` / `[x]` / `[X]`
> marker → `write_page(page, new_body, if_match=<read_etag>)`.
> The wikilink-based matching uses the same logic the editor uses:
> the wikilink must resolve to a `position` / `linecolumn` /
> `anchor` target (per the rendering rule in SB's client); the
> bridge uses `str.find`-based substring matching on the bullet
> text since wikilink-resolution semantics are not exposed over
> HTTP — if the bullet contains `[[<ref>]]` anywhere on the line,
> it's a match.
>
> `state` is `"done"` (default, flips to `[x]`), `"todo"`
> (flips to `[ ]`), or `"cancelled"` (flips to `[X]`). Any other
> value is `ToolError("state must be one of: done, todo,
> cancelled")`.
>
> **Errors**: page not found → standard 404 ToolError. No bullet
> on the page contains `[[<ref>]]` →
> `ToolError("no task with ref {ref} on page {page}; the task may
> not have a wikilink ref or may live on a different page")`.
> Multiple bullets contain `[[<ref>]]` →
> `ToolError("ref {ref} matches multiple tasks on page {page};
> narrow the ref or use patch_page_lines directly")`. Stale
> etag → standard 412 ToolError.
>
> **Wire shape**: same as T23's write tools
> (`{name, etag, size_bytes, last_modified_ms, created_ms}`).
>
> **Done when**: Layer-1 tests cover: happy path (state transitions
> all three directions), no-bullet-with-ref, multi-bullet-with-ref,
> page not found, stale etag, dry-run mode (T26-style preview
> without writing). Live e2e covers the round-trip against a
> kanban board or journal page.
>
> **Files when resolved**: `src/mcp_silverbullet/journal.py` (new
> `_find_task_bullet(body, ref) -> (line, original_state, text) |
> None`; new `check_task` tool handler; the per-page form is
> always available, the space-walk form is gated by the journal
> tools env vars from T10), `tests/test_journal_read.py` or a new
> `tests/test_tasks.py` if the journal-read file gets crowded.
>
> **Blocks on**: T23 (the return shape), T29 (the matching logic
> is shared). **Unblocks**: none.

## Not yet specified

<!-- dim view of what's coming: things we suspect we'll ticket but
can't yet phrase precisely -->

- **Batched reads/writes (`read_pages(names[])`, `write_pages(…)`)** —
  specifiable when an agent's actual workflow pays for N round trips
  and a measured client (Grok on the web) charges per call.
- **Frontmatter helpers** (`get_frontmatter(name, key?)`,
  `set_frontmatter(name, key, value, if_match?)`,
  `merge_frontmatter(name, updates, if_match?)`) — YAML hand-roll is
  fragile; needs a real YAML dep or a more careful shape. Punt to
  v1.4.
- **Trash layer / soft delete + `restore_page`** — v1's `delete_page`
  is hard delete; the agent composes `read_page → write_page(<backup>)
  → delete_page` itself today. Real undo is a v1.3 candidate.
- **Multi-page operations** (`move_pages(renames[])`,
  `delete_pages(names[])`, `patch_many({name, find, new_string}[])`)
  — same atomicity-caveat pattern as the singular `move_page`.
- **Pagination on `list_pages`** — today the entire space is
  returned in one chunk; a multi-thousand-page space makes the
  response multi-MB. Cursor-paged response is the right answer but
  needs a measured pain point.
- **Locking primitive** (`lock_page(name, ttl_s)` /
  `unlock_page(name)`) — protects an agent's edit window against a
  faster concurrent agent. Real concurrency primitive; different
  design effort.
- **Idempotency keys on writes** — today a retried call does two
  writes (the second fails 412 if the first landed). A header that
  the bridge recognizes ("I already did this") would let it return
  the prior result without re-issuing. Protocol-level concern.
- **Auto-migrate bullets to add wikilink refs** — destructive; the
  user didn't ask for it; explicitly out of scope for v1.2.
  Specifiable when a real "I want to retroactively address my old
  kanban bullets" workflow appears.
- **Token-level / word-level diff** in `diff_pages` — v1.2 ships
  line-based; finer-grained diffing is a v1.3 refinement.
- **Migration guide for clients pinned to v1.1 wire shapes** — when
  v1.2 lands, the wire shape for every read/write tool changes; a
  short CHANGELOG / migration note for downstream consumers (Grok,
  `mcp` CLI users) is needed. Could be a T23 drive-by or its own
  ticket if the migration gets non-trivial.

## Out of scope

<!-- Work ruled beyond this map's destination. Closed/fog items go in
"Decisions so far" or "Not yet specified" respectively; this section
is for *scope* boundaries. -->

- **`/healthz` endpoint** — operator-facing deploy probe. Was on the
  v1.1 chart's original T14, demoted when v1.1 redrew. v1.2 continues
  to punt; not an agent-facing need.
- **`scopes_supported` in the discovery doc** — one-line
  `AuthSettings(required_scopes=[...])` change. Server-operator
  polish, not agent-facing. Punt.
- **`allowed_origins` env var** — browser-side MCP clients. Punt.
- **`json_response=True` mode** — non-streaming clients. Punt.
- **PR to nixpkgs upgrading `python3Packages.mcp` to v2.x** — same
  punt as the prior maps.
- **Server-pushed notifications / `subscriptions/listen`** — Punt.
- **OAuth 2.1, dynamic-client registration, multi-user.** Locked
  out at T2 of the prior map.
- **Re-deciding design questions locked in `docs/design.md`.** Same
  boundary as the prior maps.
- **Journal-surface write paths.** Read-only carries forward; the
  bullet primitives live on the read side too (`list_tasks` is
  read-only, `check_task` writes through `write_page`, not through
  a journal-specific write path — the existing journal write
  constraint holds).
- **A standalone trash layer / soft delete.** Same boundary as
  the v1.1 map; `delete_page` is hard delete.
- **Markdown-aware patch.** Line-range and string-replace work on
  raw text; the bridge does not parse markdown ASTs.

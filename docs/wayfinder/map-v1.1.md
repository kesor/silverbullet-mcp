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
Missing: `delete_page`, `append_to_page`, line-range patch, string
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
- new: `delete_page`, `append_to_page`, `patch_page_lines`,
  `patch_page_replace`, `move_page`

After T21 (this session) the bridge ships seven of the eight.
`move_page` (T22) remains the only ticket ahead.

Each new tool wraps the `/.fs` HTTP primitives (GET / PUT /
DELETE) with `if_match` plumbing so the agent can edit a page
across multiple tool calls without a lost-update window. The
journal surface (T10–T13) is unchanged.

### Status

T18 (commit `d9c07`), T19 (commit `aa369`), T20 (commit pending),
and T21 (commit pending) resolved; the bridge now exposes seven
`/.fs`-backed tools — `read_page`, `write_page`, `delete_page`,
`append_to_page`, `patch_page_lines`, `patch_page_replace`,
`list_pages` — plus the resource template. T20 was the line-range
patch ticket (1-indexed inclusive ranges, trailing-newline
preservation, out-of-range pre-read validation); T21 is the
literal-substring find-and-replace counterpart (the
read-modify-write shape T19 introduced, no line-splitting code
shared). **T22 (`move_page`) is the only ticket remaining**;
it's now unblocked (T18 delivered `delete_page`, T19 delivered
the read-modify-write shape, T21 showed the inline-handler
pattern composes cleanly — T22 builds a `read → write_new →
delete_old` dance on the same primitives). T14–T17 from the
original "tunnel-ready v1.1" chart remain demoted to **Out of
scope**.

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
    (unconditional). When provided, the bridge forwards the value
    verbatim to SB's `If-Match` header (PUT or DELETE) and lets SB
    enforce the precondition — the bridge does NOT auto-fetch the
    page first. The atomicity story is "caller does the read,
    threads the etag back into its own next call": a concurrent
    agent's stale etag fails 412 at SB; the bridge doesn't
    arbitrate between them. Good enough for single-agent workflows
    and concurrent-agent protection (the second agent's write
    fails with 412 if the first one landed in between).
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

- **Drive-by (pre-chart): 412 ToolError wording.** (commit `adf0c`):
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
  (commit `adf0c`, the same commit as the 412-wording drive-by
  above — both fixes landed together): the previous shape returned `""` when the
  SB response didn't carry an `ETag` header. `sb_client.
  write_page` is now typed `-> str | None`; `server.write_page`
  mirrors it. The MCP wire payload becomes
  `{"result": null}` for the no-etag case (vs. `{"result":
  ""}` before — same shape semantically but the type now
  matches the documentation). New Layer-1 test guards the
  null-ETag case so a future refactor doesn't regress it.
  Test count: 97 pass + 2 skip (1 new test).
- **T18. `delete_page(name, if_match?)`** (commit `d9c07`):
  new MCP tool that wraps `DELETE /.fs/{name}` and brings the
  bridge to four `/.fs`-backed tools. `sb_client.delete_page`
  sends `X-Source: external` (the design-doc DELETE row's
  documented envelope) and optional `If-Match` from a new
  `_DELETE_HEADERS` constant — deliberately NOT reusing
  `_WRITE_HEADERS`, because PUTs need `X-Permission: rw` and
  DELETEs are not documented to require it; keeping the
  constants separate means a future SB tightening DELETEs
  won't be confused by an unsolicited header. Return shape
  mirrors `write_page` (`str | None`): the SB response's
  `ETag` (the deleted body's hash) echoes back so the caller
  can confirm what got removed, or `None` if the proxy
  stripped the header. New handler in `register_tools` maps
  `PageNotFound` → `ToolError("page not found: {name}")`,
  `PreconditionFailed` → `ToolError("precondition failed;
  check if_match/if_none_match")`, `ServerError` /
  `httpx.TimeoutException` per the existing shared wording.
  412 + `if_match="*"` is the interesting SB-side semantic:
  SB treats "*" as "must exist", so a missing page returns
  412 (not 404) at the SB layer; the bridge surfaces both
  with the unified 412 ToolError so callers don't need to
  distinguish "missing" from "stale etag" — they just got
  refused; they can `read_page` to figure out which. Two
  412 surfaces covered in tests: `if_match="*"` and
  `if_match=<stale_etag>`. 9 new Layer-3 tests in
  `tests/test_sb_client.py` (round-trip + etag round-trip,
  HTTP method, `if_match` *and etag* forward, no-ETag
  → `None`, 404, 412 with `*`, 412 with stale, 5xx) + 7 new
  Layer-1 tests in `tests/test_tools_in_memory.py` (200 +
  etag, `None` round-trip, `if_match="*"` forward, 404
  wording, 412 + `*` wording, 412 + stale wording, 5xx
  wording). Also touched `tests/test_journal_gate.py`
  (extended `SB_TOOL_NAMES` from 3 to 4 elements) and
  `tests/test_http_auth.py` (sorted tool-names assertion
  updated to include `delete_page`); both carry the prior
  shape forward verbatim — no behavior change. **Drive-by**: corrected
  the standing preference for `if_match` plumbing
  ("the bridge forwards the value verbatim to SB… the
  bridge does NOT auto-fetch the page first") — the
  original wording suggested an auto-fetch dance that
  neither `write_page` nor `delete_page` implements; the
  caller's read-then-write-thread-etag pattern is the
  real contract. Test count: 113 pass + 2 skip (+16).
  `nix flake check` green.
- **Drive-by: `_translate_sb_errors` helper (commit `21517`).**
  Pure refactor of `server.py`: the four existing tool handlers
  (`read_page`, `write_page`, `delete_page`, `list_pages`)
  each repeated the same `try/except` chain mapping SB
  exceptions to `ToolError` with the design doc's exact
  wording. T19 was about to add a fifth copy, so factored
  the translation into an async context manager
  `:func:`_translate_sb_errors`. The 404 wording still needs
  `name` (the page the caller asked for), so each handler
  passes its `name` through; `list_pages` passes an empty
  string (it doesn't actually raise `PageNotFound` on its
  current code path, so the wording never surfaces there).
  Pure refactor: 113 pass + 2 skip, no behavior change on
  the wire. `nix flake check` green.
- **T19. `append_to_page(name, text, if_match?)`** (commit
  `aa369`): fifth `/.fs`-backed tool, the read-modify-write
  pattern T20 and T21 build on. Renamed from the ticket's
  original `append_page` to `append_to_page` (the verb
  reads naturally as "append <text> to <page>" rather than
  "append a page" — the latter could read as "create a new
  page"). Three design calls baked into the implementation:
  (a) empty `text` rejected upfront with
  `ToolError("text must not be empty")` *before* the inner
  `sb_client` calls, saving the round trip and surfacing the
  likely caller bug at the call site; (b) one newline
  separator when the body doesn't end in one (empty body →
  `text` verbatim, body ending in `\n` → no double
  separator, body without trailing newline → exactly one
  separator) — locked down by four independent separator
  tests; (c) `if_match="*"` is a *must-exist* precondition,
  not a *create* (the read happens unconditionally and a
  missing page surfaces as the standard 404 ToolError; the
  create semantic lives on
  `write_page(name, content, if_match="*")` — keeping the
  two semantics distinct avoids the conflation the
  ticket's "treat missing as empty body" alternative
  would invite). `if_match` is forwarded to the *write*,
  not the read (the read carries no precondition); wire
  shape is `str | None` (the new ETag or `None` if SB's
  response didn't carry one), same contract as
  `write_page` / `delete_page`. 14 new Layer-1 tests in
  `tests/test_tools_in_memory.py` cover the happy-path
  roundtrip with read-then-write ordering, the separator
  rule, empty-text rejection (no GET, no PUT), `if_match`
  threading, `if_match="*"` semantics, and the 404 / 412 /
  413 / 5xx / no-ETag wire shapes. Two carry-forwards:
  `tests/test_journal_gate.py` `SB_TOOL_NAMES` extended
  from 4 to 5 elements; `tests/test_http_auth.py` sorted
  tool-names assertion updated. Drive-by live e2e:
  `tests/test_e2e_live_sb.py` grew a 20-line
  `append_to_page` roundtrip assertion in the existing
  live write/read/precondition/list_pages flow; the
  marker body's `\n` ending exercises the
  no-extra-separator branch against real SB. README tool
  list and Pi-MCP wiring paragraph now say "five tools";
  `docs/design.md` § Tools table grew by `append_to_page`
  and `delete_page` (the latter was already shipped but
  the design doc still listed it as out-of-scope-for-v1
  — cleaned up alongside T19). Test count: 127 pass + 2
  skip (+14). `nix flake check` green.
- **T20. `patch_page_lines(name, start_line, end_line, new_content, if_match?)`** (commit pending): sixth `/.fs`-backed tool, the line-range patch companion to `append_to_page`. New module-level helpers `_split_body_lines` and `_apply_line_patch` in `server.py` (line splitting + range replacement, factored out so future patch tools and tests can target them directly). Five design calls baked in: (a) split on `\n` (single newline, not `str.splitlines()`) — SB stores LF and `splitlines()` would silently strip `\r` from a stray CRLF body; the universal-newline test pins this down; (b) drop a trailing empty element from the split so line counts match an editor's "go to end" (`"a\nb\n"` is 2 lines, not 3); (c) preserve the page's trailing newline iff the body had one and the patched result is non-empty (an empty patched body has no trailing newline either way — mirrors editor semantics); (d) `start_line < 1`, `end_line < start_line` are pre-read validation errors (terse wording, no page-line-count known yet); (e) `end_line > line_count` is post-read validation with the page's line count in the wording — the ticket's recommended `"line range {start}..{end} out of bounds for page with {N} lines"` shape. Empty `new_content` deletes the range (standing preference: no separate `delete_lines` tool). `if_match="*"` and `<stale_etag>` semantics match `append_to_page` and `write_page` (forwarded to the write, not the read; 412 surfaces the unified wording). Type guards reject `bool` instances (Python's `bool` is a subclass of `int`, so `True`/`False` would otherwise sneak through as `1`/`0` line numbers). New `@mcp.tool("Patch page (lines)")` handler in `register_tools`; the 6-tool inventory assertions in `tests/test_journal_gate.py` (`SB_TOOL_NAMES`) and `tests/test_http_auth.py` (sorted `list_tools()` shape) carry forward. Drive-by: tightened `append_to_page`'s tool description (the prior wording claimed the tool's separator behavior "so callers that pass leading newlines get exactly one separator", which was misleading — the test `does_not_double_separator` documents the actual two-newline result when the caller does pass a leading `\n`). Live e2e round-trip: `patch_page_lines(name, 1, 1, "patched\n")` against body `"hello from T7 live e2e\nappended\n"` yields `"patched\nappended\n"` (the trailing `\n` from the original body is preserved). 21 new Layer-1 tests in `tests/test_tools_in_memory.py` cover: happy path, replace first/middle/last/all lines, empty `new_content` deletes range, trailing-newline preservation (both branches), `new_content` trailing-newline no-double-up, empty-body edge case (every range is out-of-bounds), single-line page, CRLF body (no `splitlines()` normalization), `if_match` plumbing (forwarded to write, not read), stale `if_match` 412, 404, 413, 5xx, no-ETag `None` round-trip, four out-of-range validation paths (`start=0`, `start=-1`, `end<start`, `end>line_count`). Test count: 148 pass + 2 skip (+21). `nix flake check` not run from this session; `pytest` runs in `.venv/` are green.
- **T21. `patch_page_replace(name, find, new_string, replace_all=False, if_match?)`** (commit pending): seventh `/.fs`-backed tool, the literal-substring find-and-replace counterpart to T20. New `@mcp.tool("Patch page (replace)")` handler in `register_tools`; lives inline in `server.py` because `str.replace` is the only logic (no line-splitting code to share with T20, no separate `_patch.py` needed). Five design calls baked in: (a) **`find` treated as a literal substring** — `str.replace`, no regex/glob/fuzzy semantics; agents that want regex match `rg` or Python `re` client-side first, then call this tool with the literal result (a regex mode would invite "I forgot to escape" disasters; the literal-vs-regex test pins this down with `find="\\d"` against body `"the \\d placeholder"` — replaces the literal backslash+d, not "any digit"); (b) **`replace_all=False` by default** — the standing preference from the map: the find string should be unique unless the caller says otherwise. Multi-match + `replace_all=False` → `ToolError("find matched N times; pass replace_all=True or narrow find")` carrying the count; (c) **`find` not in body → `ToolError("find not found in body")`** — a silent no-op would mask a typo in the find string and look like success; (d) **empty `find` rejected upfront** → `ToolError("find must not be empty")` *before* the read (`str.replace("","X","abc")` is `"XaXbXcX"`, almost never what the caller wanted; mirrors `append_to_page`'s empty-text guard); (e) **`if_match` forwarded to the write, not the read** — same contract as T19/T20; `if_match="*"` semantics carry forward. Wire shape `str | None` (the new ETag, or `None` if SB's response didn't carry one — same contract as every other write tool). 18 new Layer-1 tests in `tests/test_tools_in_memory.py` cover: happy-path roundtrip with read-then-write ordering, default `replace_all=False` replaces the unique match, multi-match + default errors (carries count), multi-match + explicit `False` errors (same wording as default), `replace_all=True` replaces every match, `find` not found (errors even with `replace_all=True`), empty `find` errors upfront (no GET, no PUT), `find` not found vs `replace_all=True` independence, literal-vs-regex (replaces `\\d` literally), empty `new_string` deletes occurrences, `find` spanning newlines, `if_match` plumbing (forwarded to write, not read), `if_match="*"` semantics, stale `if_match` 412, 404, 413, 5xx, no-ETag `None` round-trip. Carry-forwards: `tests/test_journal_gate.py` `SB_TOOL_NAMES` extended from 6 to 7 elements; `tests/test_http_auth.py` sorted tool-names assertion updated; `tests/test_e2e_live_sb.py` grew a 15-line `patch_page_replace` roundtrip in the existing live flow (body `"patched\nappended\n"` → replace `patched` with `hello` → `"hello\nappended\n"`). Drive-by: `server.py` module docstring updated from "six `/.fs`-backed tools" to "seven"; `MCPServer.instructions` reworded to list all seven tool names; `register_tools` docstring updated from "five `/.fs`-backed tools" to "seven"; `README.md` tool list grew by `patch_page_replace` (with the safe-default explanation) and the Pi-MCP wiring paragraph now says "seven tools"; `docs/design.md` § Goals updated (mentions the seven verbs), § Tools table grew by `patch_page_replace` row with the literal-substring + safe-default note, and the "What we are not doing" list dropped the `patch_page_replace — not built yet` line. Test count: 166 pass + 2 skip (+18). `nix flake check` not run from this session; `pytest` runs in `.venv/` and `nix develop` are both green.

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
> **Assignee**: `minimax-m3`
> **Status**: ✅ resolved (commit `d9c07`; see Decision above)
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
>
> **Resolution**: new method `SBClient.delete_page(name,
> if_match=None) -> str | None` issues `DELETE /.fs/{name}`
> with `_DELETE_HEADERS = {"X-Source": "external"}` (separable
> from `_WRITE_HEADERS` — the design-doc DELETE row does not
> list `X-Permission: rw`) and optional `If-Match`. Returns
> the response `ETag` (echoed by SB so the caller can confirm
> what got removed), or `None` if the response didn't carry
> one (`str | None` mirrors `write_page`'s new typing). New
> `@mcp.tool("Delete page")` handler in `register_tools`
> translates `PageNotFound` → `ToolError("page not found:
> {name}")`, `PreconditionFailed` → `ToolError("precondition
> failed; check if_match/if_none_match")` (same wording as
> `write_page`), `ServerError` / `httpx.TimeoutException`
> per the existing shared wording. No `BodyTooLarge` branch:
> DELETE has no body. 9 new Layer-3 tests + 7 new Layer-1
> tests cover: 200 + etag, no-ETag `None` round-trip,
> `if_match="*"` forward (and 412), `if_match=<stale_etag>`
> (and 412), 404, 5xx. Plus two carry-forwards: extended
> `SB_TOOL_NAMES` in `tests/test_journal_gate.py` from 3 to
> 4 entries, and the sorted tool-list assertion in
> `tests/test_http_auth.py`. Front-of-map Status block and
> Decisions-so-far updated to record this.

---

### T19. `append_to_page(name, text, if_match?)`

> **Labels**: `wayfinder:task`
> **Type**: AFK
> **Assignee**: `minimax-m3` (claimed 2026-08-29, resolved same day)
> **Status**: ✅ resolved (commit `aa369`)
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
> Either reject the combination (`ToolError("append_to_page with
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
>
> **Resolution**: New `@mcp.tool("Append to page")` handler in
> :func:`register_tools` does the read-modify-write inline —
> no new ``sb_client`` method, no new layer. Three design
> calls baked into the implementation (the ticket offered
> recommendations for two and punted the third):
>
> - **Empty ``text`` rejected upfront.**
>   ``append_to_page(name, '')`` raises
>   ``ToolError("text must not be empty")`` *before* the inner
>   ``_translate_sb_errors`` block; no GET, no PUT. An empty
>   append is almost certainly a caller bug; surfacing it
>   loudly saves the round trip and pinpoints the bug.
>
> - **Separator rule:** ``body = ''`` → ``new_body = text``;
>   ``body.endswith('\\n')`` → ``new_body = body + text``;
>   otherwise → ``new_body = body + '\\n' + text``. Exactly
>   one separator in all cases, never two. The four
>   separator-related Layer-1 tests pin this down
>   independently of the read/write plumbing.
>
> - **``if_match='*'`` is a *must-exist* precondition**, not a
>   create. The read happens unconditionally; a missing
>   page surfaces as the standard 404 ``ToolError``. The
>   *create* semantic lives on ``write_page(name, content,
>   if_match='*')`` — keeping the two semantics distinct
>   avoids the conflation the ticket's "treat missing as
>   empty body" alternative would invite. The
>   ``if_match='*'`` test pins this: the read sees an
>   existing page, the write carries ``If-Match: *``, both
>   succeed.
>
> Wire shape: ``str | None`` (the new ETag for the body, or
> ``None`` if SB's response didn't carry one — same
> ``str | None`` contract as ``write_page`` / ``delete_page``).
> ``{"result": null}`` for the no-etag case,
> ``{"result": "<etag>"}`` for the happy path. The
> ``if_match`` is forwarded to the *write*, not the read;
> the read carries no precondition.
>
> Drive-by that landed in the commit immediately before
> T19: :func:`_translate_sb_errors` — an async context
> manager that maps SB exceptions to ``ToolError`` with
> the design doc's wording. The tool handler wraps both
> inner ``sb_client`` calls in this helper, so 404 / 412 /
> 413 / 5xx / timeout all surface with the same wording as
> the four existing tools. The refactor was committed as a
> drive-by because T19 made the duplication a fifth copy;
> 113 tests stayed green across the refactor.
>
> New Layer-1 coverage in ``tests/test_tools_in_memory.py``
> (14 new tests): happy-path roundtrip with read-then-write
> ordering, separator rule (insert when body lacks newline,
> no double separator, multiple trailing newlines, empty
> body, text with leading newline), empty-text rejection
> upfront (no GET, no PUT), ``if_match`` threaded to the
> write (not the read), ``if_match='*'`` semantics, 404 /
> 412 / 413 / 5xx / no-ETag wire shapes.
>
> Test count: 127 pass + 2 skip (the two env-gated live
> tests); up from 113 + 2 skip (+14 new Layer-1 tests).
> Drive-by live e2e: ``tests/test_e2e_live_sb.py`` grew a
> 20-line ``append_to_page`` roundtrip assertion in the
> existing live write/read/precondition/list_pages flow;
> the marker body's ``\\n`` ending exercises the
> no-extra-separator branch against real SB.
>
> Rename note: the user renamed the tool from ``append_page``
> (the ticket's original name) to ``append_to_page`` because
> the verb reads naturally as "append <text> to <page>"
> rather than "append a page" (which could read as "create
> a new page"). Map heading, code, tests, README, and
> design-doc table all carry the new name; the *standing*
> preferences block refers to "append_to_page" verbatim.

---

### T20. `patch_page_lines(name, start_line, end_line, new_content, if_match?)`

> **Labels**: `wayfinder:task`
> **Type**: AFK
> **Assignee**: `minimax-m3` (claimed and resolved same session)
> **Status**: ✅ resolved (commit pending; see Decision above)
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
>
> **Resolution**: helpers `_split_body_lines` (returns
> `(lines, had_trailing_newline)`) and `_apply_line_patch`
> (replaces the 1-indexed inclusive range) live at module level
> in `server.py` — not in a separate `_patch.py`, because T21
> (`patch_page_replace`) is the only other potential consumer
> and it doesn't need line splitting (it's a literal substring
> swap). New `@mcp.tool("Patch page (lines)")` handler in
> :func:`register_tools` does the read-modify-write inline,
> matching the T19 / T18 shape. Type guards reject `bool`
> inputs (Python's `bool` is a subclass of `int`; without the
> guard, `True`/`False` would silently become `1`/`0` line
> numbers). Pre-read validation errors (`start_line < 1`,
> `end_line < start_line`) use terse wordings without the page
> line count (it isn't known yet); the post-read
> `end_line > line_count` error carries the count, matching
> the ticket's recommended wording verbatim. The trailing-
> newline preservation is a small editor-style courtesy: the
> split+rejoin cycle drops the body's trailing `\n`, so the
> tool re-attaches it iff the body had one and the result is
> non-empty. Drive-by: tightened :func:`append_to_page`'s tool
> description (the prior wording claimed the separator
> behavior "so callers that pass leading newlines get exactly
> one separator", which contradicts the
> `does_not_double_separator` test — the new wording is
> precise). 21 new Layer-1 tests in
> `tests/test_tools_in_memory.py` cover every case listed in
> the ticket's "Done when" plus the CRLF, empty-page, and
> single-line edge cases, the `bool` type guard, the
> null-ETag round-trip, and the four out-of-range validation
> paths. `tests/test_journal_gate.py` (`SB_TOOL_NAMES`) and
> `tests/test_http_auth.py` (sorted `list_tools()` shape) carry
> forward with the new tool name. Test count: 148 pass + 2
> skip (+21). `nix flake check` not run from this session; the
> `.venv/` pytest run is green.

---

### T21. `patch_page_replace(name, find, new_string, replace_all=False, if_match?)`

> **Labels**: `wayfinder:task`
> **Type**: AFK
> **Assignee**: `minimax-m3` (claimed 2026-08-29, resolved same day)
> **Status**: ✅ resolved (commit pending; see Decision above)
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
>
> **Resolution**: New `@mcp.tool("Patch page (replace)")`
> handler in :func:`register_tools` does the read-modify-write
> inline — no new ``sb_client`` method, no separate ``_patch.py``
> (T20's helpers are line-splitting-specific; ``str.replace`` is
> the only logic here). Five design calls baked into the
> implementation, all aligned with the standing preferences and
> the ticket body:
>
> - **Empty ``find`` rejected upfront.**
>   ``patch_page_replace(name, "", "X")`` raises
>   ``ToolError("find must not be empty")`` *before* the inner
>   ``_translate_sb_errors`` block — no GET, no PUT. Same pattern
>   as :func:`append_to_page`'s empty-text guard. Pinning the
>   behavior down keeps the safe default unambiguous: ``""``
>   matches between every character (``"abc".replace("", "X")``
>   is ``"XaXbXcX"``), which is almost never what the caller
>   wanted.
>
> - **Literal substring, no regex.** ``body.replace(find,
>   new_string, count)`` — Python's ``str.replace`` is
>   substring-based, no escaping required. The literal-vs-regex
>   test pins this down with ``find="\\d"`` against body
>   ``"the \\d placeholder"``: the literal two-character
>   substring gets replaced, not the regex character class.
>   Agents that want regex match ``rg`` or Python ``re``
>   client-side first, then call this tool with the literal
>   result (a regex mode in the bridge would invite "I forgot to
>   escape" disasters, per the standing preference).
>
> - **Safe default: ``replace_all=False``.** When the find
>   matches more than once, the tool raises
>   ``ToolError("find matched N times; pass replace_all=True or
>   narrow find")`` carrying the match count, *before* the
>   write. The read still happens (we needed it to count
>   matches); the write is skipped. This is the standing
>   preference ("the find string should be unique unless the
>   caller says otherwise") and keeps a typo from silently
>   mass-editing — the caller sees the count and chooses to
>   narrow ``find`` or opt in.
>
> - **``find`` not in body → ``ToolError("find not found in
>   body")``.** A silent no-op would mask a typo in the find
>   string and look like success. The error fires *before* the
>   replace_all branch — so a caller who blindly flips
>   ``replace_all=True`` hoping to recover from a typo gets the
>   same loud failure they would have gotten with the default.
>
> - **``if_match`` forwarded to the write, not the read.** Same
>   contract as :func:`append_to_page` and
>   :func:`patch_page_lines`. ``if_match="*"` requires the page
>   to exist (the write layer checks; the read happens
>   unconditionally, which is correct because the read is
>   counting matches, not guarding).
>
> Wire shape: ``str | None`` (the new ETag for the body, or
> ``None`` if SB's response didn't carry one — same
> ``str | None`` contract as the other write tools).
> ``{"result": null}`` for the no-etag case,
> ``{"result": "<etag>"}`` for the happy path.
>
> Bonus edge case tested: ``find`` substrings that span ``\n``
> work (``str.replace` is substring-based; the body is a flat
> string from the bridge's perspective — no line-splitting
> semantics, intentionally distinct from T20's line-indexed
> shape). Empty ``new_string`` deletes the occurrences (same
> standing-preference rule as T20's empty ``new_content``
> deletes the line range).
>
> 18 new Layer-1 tests in ``tests/test_tools_in_memory.py``
> cover every case listed in the ticket's "Done when" plus the
> literal-vs-regex, empty-``new_string``-deletes, and
> find-spans-newlines edges, the ``replace_all=True`` +
> not-found independence, and the null-ETag round-trip.
> ``tests/test_journal_gate.py`` ``SB_TOOL_NAMES`` extended
> from 6 to 7 entries; ``tests/test_http_auth.py`` sorted
> tool-names assertion updated. Drive-by live e2e:
> ``tests/test_e2e_live_sb.py`` grew a 15-line
> ``patch_page_replace`` roundtrip in the existing live flow
> (body ``"patched\nappended\n"`` → replace ``patched`` with
> ``hello`` → ``"hello\nappended\n"``).
>
> Drive-bys that landed in this session: ``server.py`` module
> docstring updated from "six ``/.fs``-backed tools" to
> "seven"; ``MCPServer.instructions`` reworded to list all
> seven tool names; ``register_tools`` docstring updated from
> "five ``/.fs``-backed tools" to "seven"; ``README.md`` tool
> list grew by ``patch_page_replace`` (with the safe-default
> explanation in plain English) and the Pi-MCP wiring
> paragraph now says "seven tools"; ``docs/design.md`` §
> Goals updated (mentions the seven verbs), § Tools table
> grew by ``patch_page_replace`` row with the
> literal-substring + safe-default note, and the "What we are
> not doing" list dropped the ``patch_page_replace — not
> built yet`` line. Test count: 166 pass + 2 skip (+18).
> ``nix flake check`` not run from this session;
> ``pytest .venv/`` and ``pytest`` under ``nix develop`` are
> both green.

---

### T22. `move_page(name, new_name, if_match?)`

> **Labels**: `wayfinder:task`
> **Type**: AFK
> **Assignee**: _(unclaimed)_
> **Status**: open (now unblocked: T18 delivered `delete_page`,
> T19 delivered the read-modify-write shape, T21 showed the
> inline-handler pattern composes cleanly)
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
  instead), `append_to_page_many`, `patch_many`. Punt.
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

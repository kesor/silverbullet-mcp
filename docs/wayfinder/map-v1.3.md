# Wayfinder Map — `mcp-silverbullet` v1.3 (agent-grade discovery + edit hygiene)

<!--
Local-markdown tracker, like the prior build maps (`map.md` for v1,
`map-v1.1.md`, `map-v1.2.md`). The v1.2 destination was "agent-facing
QOL + bullet primitives" and reached it when T30 closed — the bridge
now exposes twelve tools plus one resource template (see
`docs/wayfinder/map-v1.2.md` ## Status). This map is the next effort.

Standing preferences from the prior maps continue to apply unless
overridden here:

- Off-the-shelf libraries only — `mcp==2.1.1`, `httpx2`, Starlette,
  uvicorn, pytest. No new Python deps for v1.3 unless a ticket
  explicitly says otherwise.
- Side-car process; one bearer secret on both hops; no daemonization;
  no OAuth 2.1 dance.
- Every write tool honors `if_match`.
- Every write tool returns the T23 ack envelope
  (`{name, etag, size_bytes, last_modified_ms, created_ms}`).
- Layer 1 (in-memory `Client(mcp)`), Layer 2 (real ASGI transport),
  Layer 3 (`httpx.MockTransport` against `sb_client`) test split from
  the v1 map's T3 / T4 / T5 carry-forwards. Live e2e stays env-gated
  per the v1 T7 shape.
- The non-goals in `docs/design.md` § Goals/non-goals continue to
  bind: OAuth 2.1, multi-user, semantic search, mutating SB's
  source, hosting the bridge for other people are out of scope.

Research inputs for this map live in
`docs/competitive-landscape.md` (feature matrix + ranked borrow list
across nine SilverBullet MCP projects and one Obsidian reference). The
priorities here follow that doc's "Ranked recommendation" section,
with the If-Match verification ticket promoted to lead because the
v1.2 concurrency story depends on it being true.

When in doubt, `docs/wayfinder/map.md` / `map-v1.1.md` / `map-v1.2.md`
are the source of truth on standing preferences; this map inherits
them.
-->

## Destination

> **v1.3: agent-grade discovery + edit hygiene.** Six tickets.
> The agent gains two new discovery tools — `search_pages` and
> `find_backlinks` — that surface information already present in
> the SB space directory but currently invisible to MCP clients
> (substring content search and wikilink-target backlinks,
> respectively, both journal-gated the same way
> `journal_histogram`/`tag_summary`/`recent_pages`/
> `pages_touching_topic` are). The edit surface gains two new
> tools that close the most common "the agent has to compose
> three primitives to do one thing" gap — `create_page` (refuse
> to overwrite, distinct from `write_page`'s overwrite-or-create
> default) and `prepend_to_page` (insert at top of body or just
> below the YAML frontmatter block). The bridge gains a 256 KB
> local body-size cap so an oversized write surfaces a clear
> `body_too_large` `ToolError` instead of deferring the failure
> to SB. And — *first*, because everything else depends on it —
> the bridge runs a live verification ticket that confirms SB's
> `If-Match` header is honored on `PUT /.fs/{name}`; if it
> isn't, the ticket switches the write tools to the
> `expected_last_modified` body-field convention from
> `xmatthewx/silverbullet-mcp-server` (T38-style compat ticket
> chain).

The shape:

- **Concurrency verification (T31)**: a single live test
  exercises a write-then-write race against a running SB instance
  and asserts the second call's `If-Match: <stale_etag>` returns
  412. If it does, T31 closes with a one-paragraph "verified"
  resolution; if it doesn't, T31 spawns two follow-up tickets
  (T31a, T31b) to switch the concurrency-token convention. The
  v1.2 / T23 envelope story depends on this being true.
- **Discovery (T34, T35)**: two new `@mcp.tool` handlers that
  reuse the existing journal fs-walk plumbing. `search_pages`
  is a thin wrapper over the v1 T12
  `pages_touching_topic` machinery with a narrower `limit?` /
  `prefix?` shape that returns the same `{name, snippet,
  match}` shape the journal tool already returns.
  `find_backlinks(target)` is new — a wikilink-target scan over
  the SB space directory (the same `[[…]]` regex `lidiaev/me-db`
  uses in `find_backlinks`, ~30 lines).
- **Edit hygiene (T32, T33)**: `create_page(name, content,
  if_match?)` is a thin wrapper over `write_page(if_match="*")`
  with the 412 path translated into a clean `already_exists`
  `ToolError("page already exists: {name}")`; same envelope
  return shape as T23. `prepend_to_page(name, content,
  position="after_frontmatter"|"top", if_match?,
  dry_run=False)` mirrors `append_to_page`'s read-modify-write
  shape but inserts at the top, with the frontmatter-aware
  default that humans actually want when they say "prepend".
- **Write-side guardrail (T36)**: a 256 KB cap on the UTF-8 byte
  count of any write-tool body, surfaced as
  `ToolError("body too large: {size_bytes} bytes exceeds 256 KiB
  cap; chunk into append_to_page calls")` before the read or
  write round trip. The cap applies to every write tool — direct
  (`write_page`) and read-modify-write (`append_to_page`,
  `patch_page_lines`, `patch_page_replace`, `move_page`,
  `prepend_to_page`, `check_task`) — but excludes the body
  payload from the bridge's internal `pages_touching_topic`-style
  reads (those are bounded by `max_results` on the journal side
  and the `/.fs` GET body on the read side).

### Status

T31 (If-Match verification) is the lead ticket and unblocks the
remaining four — every ticket from T32 onward assumes that
`If-Match` on PUT/DELETE is honored by SB, which the v1.2 design
had to take on faith. T32 (`create_page`) and T33
(`prepend_to_page`) are independent of T31's outcome once T31
closes positively. T34 (`search_pages`) and T35 (`find_backlinks`)
are independent of T31 and of each other; they reuse the v1
journal surface and only need the journal config to be on (same
gate as `journal_histogram` etc.). T36 (body-size cap) is also
independent of T31 and ships behind a test that runs without a
live SB.

If T31 closes negatively — i.e., SB does not honor `If-Match` on
`PUT /.fs/{name}` — the map pivots: T31a (change
`sb_client.write_page` to take `expected_last_modified` as a body
field rather than `If-Match` as a header) and T31b (do the same
for `delete_page` and the read-modify-write tools) are added as
follow-ups, and the T32/T33/T36 tests have to be re-pointed at
the new convention. T34/T35 (the discovery tools) are unaffected
because they're read-only.

## Notes

- **Domain**: same as the prior maps (protocol bridge). v1.3
  stays inside the existing MCP-SB boundary — no new transports,
  no new auth hop, no new dependencies.
- **Skills every session should consult**: `mattpocock/skills@grilling`,
  `mattpocock/skills@domain-modeling`, `incremental-implementation`,
  `security-and-hardening`. The prior maps' standing preferences
  continue to bind.
- **Standing preferences for this effort** (continuing from the prior
  maps):
  - **No new Python dependencies.** Every ticket reuses what's
    already in `sb_client.py` / `journal.py` / `server.py`. If a
    ticket needs a YAML parser or a markdown AST, it does NOT
    pull one in — it works on raw text the way SB stores it. The
    design doc § Goals/non-goals lists "off-the-shelf libraries
    only" and the prior maps have all respected it.
  - **Discovery tools follow the journal gate.** T34
    (`search_pages`) and T35 (`find_backlinks`) are
    fs-walk tools; they ship only when
    `MCP_SILVERBULLET_JOURNAL_TOOLS=1` *and*
    `MCP_SILVERBULLET_SPACE_PATH` are both set, same as the v1
    `journal_histogram` / `tag_summary` / `recent_pages` /
    `pages_touching_topic` set. Without both env vars, the bridge
    boots cleanly without the discovery tools and logs a single
    `INFO`/`WARN` line, the way the journal surface already
    does.
  - **`create_page` is `write_page` with a refusal, not a
    parallel implementation.** T32 delegates to
    `sb_client.write_page(name, content, if_match="*")` and
    translates the resulting 412 into a clean `already_exists`
    `ToolError`. The new tool exists because the agent
    experience of "I tried to create, got a 412-equivalent, now
    I need to pattern-match on the error string" is the single
    most common friction point in the v1.2 surface. One
    `@mcp.tool` handler + one error-translation block + tests.
  - **`prepend_to_page` defaults to "after frontmatter".** The
    `xmatthewx/silverbullet-mcp-server` default. A human who
    says "prepend" to a journal page wants the new content
    *above* the page body and *below* the YAML frontmatter
    block; top-of-page-everything is a different (rare) intent
    and gets its own `position="top"` knob. The two-mode shape
    mirrors `append_to_page`'s `dry_run` knob — small extra
    surface, big usability win.
  - **Body-size cap is local, not SB-deferred.** T36 measures
    the UTF-8 byte count of the just-written body *before* the
    PUT (so the read step on read-modify-write tools is
    unaffected). The cap matches `xmatthewx`'s 256 KiB — small
    enough to surface clearly to an agent as "you tried to
    write 600 KB to a journal page, that won't fit, here's the
    remediation hint", large enough to be a non-issue for any
    human-authored page. The cap does *not* apply to
    `read_page`'s body (a 1 MiB page is fine to read; the agent
    can choose to `append_to_page` chunks instead). Tests use a
    257 KiB sentinel body so the boundary is exact.
  - **If-Match verification (T31) is the operational canary.**
    The v1.2 design has been assuming since T23 that
    `If-Match: <etag>` works on `PUT /.fs/{name}`. That
    assumption has not been tested against a real SB. T31 is a
    single pytest test that creates a page, reads it twice,
    issues a write with the first read's etag, then a second
    write with the first read's etag (which is now stale), and
    asserts the second call returns 412-equivalent ToolError
    rather than succeeding. Live SB required (env-gated per the
    v1 T7 pattern; without `MCP_SILVERBULLET_LIVE_SB_URL` the
    test skips). If the test passes, T31 closes positively and
    the assumption is verified. If it fails, T31 closes
    negatively and spawns T31a/T31b; the rest of the map
    continues but with the new concurrency-token convention.
  - **Backlink rewrite on rename is still out of scope.**
    `xmatthewx` documented this as unreachable
    (`Page: Rename` is a client-side editor command, not an
    HTTP API). We agree; `move_page` (v1.1's T22) does NOT
    rewrite `[[backlinks]]`. v1.3 doesn't revisit this.
  - **Live-SB tests stay env-gated**, same shape as the v1 T7
    and v1.1 T19 / T21 / T22 and v1.2 T23 / T28 / T30
    carry-forwards. T31 is *the* live-SB test; the rest can be
    Layer 1/2/3.

## Decisions so far

<!-- index only — one line per closed ticket, link to the ticket's
resolution below -->

*(no tickets resolved yet — this map is in the lead-blocked-on-T31
state. Resolutions appear here as T31 onward close.)*

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

### T31. Verify SB honors `If-Match` on `PUT /.fs/{name}`

> **Labels**: `wayfinder:task`, `wayfinder:verification`
> **Type**: AFK
> **Status**: 🟡 open — lead ticket
> **Question**: Does SB actually enforce `If-Match: <etag>` on
> `PUT /.fs/{name}`, or has the v1.2 design been assuming a
> behavior we never tested?
>
> **Context**: The v1.2 design's whole concurrency story rides
> on the agent passing `If-Match: <read_etag>` back on a
> follow-up write; a concurrent edit from a second agent between
> the read and the write should cause the second agent's stale
> etag to fail at SB rather than silently overwriting. The v1
> map's T7 (live SB e2e) ran a basic read-write-read round trip
> but never tested the conflict path. v1.2's T23 added the
> `last_modified_ms` / `etag` fields to the write envelope, but
> the assumption that SB will return 412 on a stale etag was
> inherited from v1.1's T18/T19 work without explicit
> verification. `xmatthewx/silverbullet-mcp-server` (one of the
> v1.3 competitive-landscape inputs) sidesteps this by using a
> body-field `expected_last_modified` instead of the HTTP
> `If-Match` header — a clue that some SB versions / proxy
> setups don't honor the header.
>
> **Goal**: produce one new live-SB pytest case that exercises a
> read-stale-write race and asserts the second write returns
> 412-equivalent ToolError (or, in the negative case, that it
> silently overwrites — which would force T31a/T31b onto the
> roadmap). The test runs only when
> `MCP_SILVERBULLET_LIVE_SB_URL` is set, per the v1 T7 gate.
>
> **Done when**: `tests/test_e2e_live_sb.py` has a new
> `test_if_match_stale_etag_returns_412` test (or equivalent)
> that runs against the live SB and either passes (positive
> resolution: SB honors `If-Match`; v1.2's design is verified)
> or fails (negative resolution: T31a / T31b get added to this
> map and the v1.2 design is corrected).
>
> **Files when resolved**:
> `tests/test_e2e_live_sb.py` (the new test),
> `docs/design.md` § SilverBullet client contract (add a
> one-line note on the If-Match row either way the test
> resolves).
>
> **Blocks on**: nothing — this is the lead ticket. **Unblocks**:
> every other ticket on this map assumes `If-Match` is honored,
> so the v1.2 design is not safe to extend (T32/T33/T36) until
> T31 closes positively.
>
> **Negative-resolution follow-ups** (only if the test fails):
> T31a. Switch `sb_client.write_page` to take
> `expected_last_modified` as a body field and emit the SB
> HTTP-equivalent via whatever transport SB actually exposes.
> T31b. Update the read-modify-write tools (`append_to_page`,
> `patch_page_lines`, `patch_page_replace`, `move_page`,
> `check_task`) to thread the same convention through.
> `xmatthewx`'s README has the body-field contract documented;
> the cutover is mechanical.

---

### T32. `create_page(name, content, if_match?)`

> **Labels**: `wayfinder:task`
> **Type**: AFK
> **Status**: 🟡 open
> **Question**: How does the bridge expose a refuse-to-overwrite
> create tool distinct from `write_page`'s overwrite-or-create
> default?
>
> **Context**: Three of the four SB-MCP competitors that ship a
> write surface (`pepomes`, `xmatthewx`, `bfeller` via the
> `create_only` flag on `write_note`) expose `create_page` (or
> the equivalent `create_only` knob) as a distinct operation
> from `write_page`. Today an agent that wants to create
> `Notes/Foo.md` only if it doesn't exist has to call
> `page_exists(name)` first, then `write_page(name, content,
> if_match="*")`, then handle the 412 path if it raced. That's
> three round trips and a pattern-match on the error string.
> Splitting `create_page` out is one new `@mcp.tool` handler
> that wraps `write_page(if_match="*")` and translates the
> 412-equivalent `ToolError` into a clean
> `ToolError("page already exists: {name}; use write_page to
> overwrite")`. The agent's script becomes two lines and the
> error message names the right next tool.
>
> **Goal**: ship a `create_page` tool that surfaces the
> refuse-to-overwrite semantics as a first-class operation, with
> the T23 ack envelope return shape so the agent has
> `size_bytes` / `last_modified_ms` / `created_ms` from the
> successful create.
>
> **Done when**: a Layer-1 test covers: happy path (create a new
> page; returns the T23 envelope; the page is now readable via
> `read_page`), page-already-exists (returns the
> `page already exists` `ToolError` rather than 412), empty
> `name` (upfront `ToolError("name must not be empty")` like the
> v1.2 tools' input-validation pattern), `if_match="*"` is
> implied (no need to make the caller pass it). Layer-2 test
> via the real ASGI transport, same shape. T7 live e2e: a
> create-on-empty round trip.
>
> **Files when resolved**:
> `src/mcp_silverbullet/server.py` (new `@mcp.tool` handler that
> delegates to `sb_client.write_page(name, content, if_match="*")`
> and translates 412 → `ToolError("page already exists: {name};
> use write_page to overwrite")`),
> `tests/test_tools_in_memory.py` (Layer-1 cases),
> `tests/test_tools_asgi.py` (Layer-2 cases),
> `tests/test_e2e_live_sb.py` (T7 live case),
> `README.md` (New tool entry in the "What it exposes" list),
> `CHANGELOG.md` Unreleased section (v1.3 added entry).
>
> **Blocks on**: T31 (the v1.2 design's concurrency story
> depends on it being true; `create_page` inherits the same
> contract). **Unblocks**: none.
>
> **Out of scope**: `create_page` does NOT support `dry_run` —
> it's a single PUT, not a read-modify-write. The
> `dry_run`-shaped concerns (preview the body, surface the
> diff) don't apply. If a future ticket wants a
> "create-with-template" semantic, it lands as its own ticket.

---

### T33. `prepend_to_page(name, content, position="after_frontmatter"|"top", if_match?, dry_run=False)`

> **Labels**: `wayfinder:task`
> **Type**: AFK
> **Status**: 🟡 open
> **Question**: How does the bridge expose a top-of-body insert
> primitive that handles YAML frontmatter correctly?
>
> **Context**: `xmatthewx/silverbullet-mcp-server` and
> `obsidian-mcp` both ship `prepend_to_page` (the latter as
> `note_insert` with `position="beginning"`). The v1.2 surface
> has `append_to_page` (bottom-insert, body-separator-aware)
> but no top-insert primitive. An agent that wants to add a
> "Quick capture" header to the top of a daily-journal page
> today has to read the body, manually split the frontmatter
> from the body, splice, and `write_page` back — a 6-step
> recipe that's the second-most-common agent friction point
> (after the `create_page` case in T32).
>
> The key design call is the frontmatter-aware default:
> "prepend" should mean "above the body content but below the
> YAML frontmatter block" (the human-meaningful default), not
> "absolute top of the file" (which puts the new content above
> the YAML, breaking frontmatter consumers that expect to find
> it at the top of the page). `xmatthewx`'s
> `position` parameter is exactly this: `after_frontmatter`
> (default) vs `top`. We mirror that shape.
>
> **Goal**: ship a `prepend_to_page` tool that mirrors
> `append_to_page`'s read-modify-write + `dry_run` shape, with
> the frontmatter-aware default.
>
> **Done when**: Layer-1 tests cover: happy path (prepend to a
> page with no frontmatter; new content at the absolute top);
> happy path with frontmatter (new content inserted between
> the closing `---` of the frontmatter and the first body
> line); `position="top"` (overrides the default; new content
> above the frontmatter); `position="after_frontmatter"` is the
> default; empty `content` upfront `ToolError("content must
> not be empty")`; `dry_run=True` returns the T26
> `{dry_run, original, patched, diff}` preview envelope
> without writing; `if_match=<stale_etag>` raises the 412
> ToolError on both live and dry-run paths. Layer-2 test via
> the real ASGI transport, same shape. T7 live e2e: prepend to
> a journal page with a YAML frontmatter; verify the
> frontmatter is still at the top and the new content is just
> below.
>
> **Files when resolved**:
> `src/mcp_silverbullet/server.py` (new `@mcp.tool` handler
> following the `append_to_page` template — read with etag,
> compute the splice per `position`, dry-run or write with
> `if_match=<read_etag>`),
> `tests/test_tools_in_memory.py` (Layer-1 cases),
> `tests/test_tools_asgi.py` (Layer-2 cases),
> `tests/test_e2e_live_sb.py` (T7 live case with frontmatter).
>
> **Blocks on**: T31 (concurrency story), T23 (the ack envelope
> return shape; `prepend_to_page` returns the same envelope).
> **Unblocks**: none directly; an optional future T33a could
> ship `dry_run=True` on `prepend_to_page` if the
> `append_to_page` `dry_run` test surface needs expansion.
>
> **Frontmatter detection**: the bridge looks for a leading
> `---\n…\n---\n` block (LF or CRLF) and treats everything up
> to the closing `---` as frontmatter. A page that opens with
> `---` but doesn't close it (a malformed frontmatter block) is
> treated as no-frontmatter — the new content goes at the
> absolute top, same as a page with no frontmatter. This is
> the same "raw text, no parser" pattern the v1.1 / v1.2 maps
> use; we do not pull in a YAML library.

---

### T34. `search_pages(query, prefix?, limit?)`

> **Labels**: `wayfinder:task`
> **Type**: AFK
> **Status**: 🟡 open
> **Question**: How does the bridge expose substring content
> search to MCP clients?
>
> **Context**: Every SB-MCP competitor that ships a read
> surface ships a search tool (`Ahmad-A0`'s `search_notes`,
> `bfeller`'s `search_notes`, `pepomes`'s `sb_search`,
> `xmatthewx`'s `search_pages`, `are/bmad-mcp-silverbullet`'s
> `search_pages`, `lidiaev/me-db`'s `search`). The v1 design
> doc lists "search" as a non-goal but in context the
> non-goal is *semantic* search (BM25 / embeddings / vector
> search) — substring content search is a natural extension of
> the v1 journal surface, which already runs `rg --json`
> against the SB space directory for `pages_touching_topic`.
> Substring search fits our stated goals ("agent can find
> pages") without breaking them ("we don't do semantic
> search").
>
> The shape reuses the v1 `pages_touching_topic` machinery:
> scan the SB space directory for `*.md` files; for each file,
> case-insensitive substring match against the filename and the
> body; emit `{name, snippet, match}` where `match` is `"name"`,
> `"content"`, or `"both"` and `snippet` is an ~80-char window
> centered on the content match. The same `rg --json` /
> pure-Python fallback from T12 carries forward.
>
> The tool is gated behind the journal surface — same
> `MCP_SILVERBULLET_JOURNAL_TOOLS=1` +
> `MCP_SILVERBULLET_SPACE_PATH` env var pair that gates
> `journal_histogram` / `tag_summary` / `recent_pages` /
> `pages_touching_topic`. Without both env vars, the bridge
> boots cleanly without the search tool and logs a single
> `INFO`/`WARN` line, the way the journal surface already does.
>
> **Goal**: ship a `search_pages` tool that delegates to the
> existing `pages_touching_topic` machinery and returns the
> same `{name, snippet, match}` shape, gated by the journal
> config.
>
> **Done when**: Layer-1 tests cover: happy path (substr in a
> single page body returns `{name, snippet: "…window…",
> match: "content"}`); name-match (`match: "name"`); both
> (page name matches AND body matches; `match: "both"`); empty
> `query` upfront `ToolError("query must not be empty")`;
> `prefix` filters the scan to files whose path starts with
> `prefix` (a "Projects/" prefix scopes to the Projects
> subtree); `limit` (default 20, hard cap 100) bounds the
> result list. Live-SB integration is not strictly needed (the
> tool is fs-direct); the v1 T13 live-journal test pattern
> carries forward if the operator wants to verify the gating
> end-to-end.
>
> **Files when resolved**:
> `src/mcp_silverbullet/journal.py` (new
> `_search_pages(query, prefix, limit)`; the journal
> registration in `register_journal_tools` adds the new
> `@mcp.tool` handler alongside `pages_touching_topic`),
> `tests/test_journal_search.py` (new Layer-1 cases; or
> extend `test_journal_read.py` if that file is the natural
> home).
>
> **Blocks on**: T10/T12 (the journal surface itself; both are
> already shipped in v1). **Unblocks**: none directly; a future
> T34a could add an optional `case_sensitive` knob (default
> False, matching the v1 `pages_touching_topic` default).
>
> **Out of scope** (intentionally):
> - BM25 ranking, semantic / vector search, embeddings —
> explicitly non-goal.
> - The `match: "name" | "content" | "both"` taxonomy follows
>   `pages_touching_topic`'s existing shape; we don't add a
>   rank or score field.
> - Pagination / cursors — v1.2's "Not yet specified" section
>   already calls out `list_pages` pagination as a v1.4+
>   concern; `search_pages` pagination is the same shape.

---

### T35. `find_backlinks(target) -> [{file, line, text}]`

> **Labels**: `wayfinder:task`
> **Type**: AFK
> **Status**: 🟡 open
> **Question**: How does the bridge surface wikilink-target
> backlinks to MCP clients?
>
> **Context**: Two of the v1.3 competitive-landscape competitors
> ship backlinks: `lidiaev/me-db`'s `find_backlinks` (regex
> `[[…]]` scan over the space directory) and `obsidian-mcp`'s
> `wikilinks` (four-way: backlinks / outgoing / broken /
> orphans). Backlinks are a fundamental SB concept — every
> page is a graph node, and the graph is what makes the
> journal / daily-notes / project-pages workflows tick.
> Today the bridge can't tell an agent "every page that links
> to `Projects/Foo`" without the agent calling
> `pages_touching_topic("[[Projects/Foo]]")` and pattern-matching
> on its own.
>
> The shape follows `lidiaev/me-db`'s contract (the closest
> match in the v1.3 competitive-landscape inputs):
> `{file, line, text}[]` where `file` is the relative path to
> the linking page, `line` is the 1-indexed line number, and
> `text` is the stripped line. The wikilink regex is
> `\[\[([^|\]]+)(?:\|[^\]]*)?\]\]` — accepts both `[[target]]`
> and `[[target|alias]]`, matching SB's own parser. The target
> is normalized: trailing `.md` stripped, leading/trailing
> slashes stripped (so `Projects/Foo`, `Projects/Foo.md`, and
> `/Projects/Foo/` all match the same canonical target). The
> scan walks the SB space directory (same `*.md` filter as the
> journal surface); excluded paths (those starting with `.` or
> `_`) match the v1 journal convention.
>
> The tool is gated behind the journal surface — same env var
> pair as T34 (`search_pages`).
>
> **Goal**: ship a `find_backlinks` tool that walks the SB
> space directory, scans every `*.md` file for wikilink
> references to the given target, and returns
> `{file, line, text}[]`.
>
> **Done when**: Layer-1 tests cover: happy path (a page with
> one `[[target]]` reference returns one entry); multiple
> references (a page with three `[[target]]` references on
> three different lines returns three entries); aliased
> references (`[[target|alias]]` matches the bare `target`
> target); target normalization (`target = "Projects/Foo"`,
> `query = "Projects/Foo.md"` matches); self-link
> (`Projects/Foo` containing `[[Projects/Foo]]` returns the
> self-link as one entry, not zero); no matches returns `[]`,
> not `ToolError` (the agent might be searching pre-emptively);
> empty `target` upfront `ToolError("target must not be
> empty")`. Live-SB integration is not strictly needed (the
> tool is fs-direct); the v1 T13 pattern carries forward.
>
> **Files when resolved**:
> `src/mcp_silverbullet/journal.py` (new
> `_find_backlinks(target)`; new `WIKILINK_RE` regex constant
> per the v1.2 journal helpers' style; new
> `_normalize_link_target` helper; the journal registration
> in `register_journal_tools` adds the new `@mcp.tool`
> handler).
>
> **Blocks on**: T10/T12 (the journal surface itself). **Unblocks**:
> none directly; an optional future T35a could ship
> `outgoing_links(name)` (the inverse direction: from the
> page, find every wikilink target) and T35b could ship
> `orphan_pages(prefix?)` (every page with zero incoming
> backlinks), but both are explicitly out of scope for v1.3.
>
> **Out of scope** (intentionally):
> - `obsidian-mcp`'s `query: "backlinks"|"outgoing"|"broken"|"orphans"`
>   shape — too much surface for v1.3. The bare `find_backlinks`
>   covers the single most common need ("I'm about to rename
>   this page, what's affected?"). Other directions land in
>   later tickets if needed.
> - Backlink rewrite on rename — already out of scope per
>   the v1.1 T22 standing preference; v1.3 doesn't revisit.
> - Block references (`[[page#block]]`) — `lidiaev/me-db`
>   matches the whole `page#block` string as a target. We
>   match the same way; the agent that wants block-level
>   precision calls `find_backlinks("page#block")` and gets
>   exactly the block references.

---

### T36. 256 KiB body-size cap on every write tool

> **Labels**: `wayfinder:task`
> **Type**: AFK
> **Status**: 🟡 open
> **Question**: How does the bridge surface a clear
> `body_too_large` error before the SB round trip?
>
> **Context**: `xmatthewx/silverbullet-mcp-server` caps write
> bodies at 256 KiB and surfaces a 413-equivalent error with a
> remediation hint. Today the bridge silently trusts SB to
> reject oversized writes; an agent that accidentally writes
> 600 KB to a journal page succeeds at the bridge layer and
> only learns the failure at SB. Capping locally means the
> agent sees one clear `body_too_large` `ToolError("body too
> large: {size_bytes} bytes exceeds 256 KiB cap; chunk into
> append_to_page calls")` *before* the round trip, with the
> remediation hint naming the right next tool.
>
> The cap applies to every write tool — direct
> (`write_page`, `create_page`, `prepend_to_page`,
> `append_to_page`, `patch_page_lines`, `patch_page_replace`,
> `move_page`) and the bullet primitives
> (`check_task`). It does NOT apply to the read side
> (`read_page`'s body can be arbitrarily large; the agent
> can choose to `append_to_page` chunks instead). It does NOT
> apply to the journal-discovery side
> (`pages_touching_topic` already has its own `limit`
> knob; `search_pages` will inherit T34's `limit` knob;
> `find_backlinks` returns metadata, not bodies).
>
> The cap is measured on the UTF-8 byte count of the body the
> bridge is about to write — not the request body's
> Content-Length, not the body the bridge just read on
> read-modify-write tools, not the page's stored
> `size_bytes`. The measurement happens *before* the PUT, so
> the read step on read-modify-write tools is unaffected (a
> 500 KB existing page is fine; a 500 KB about-to-be-written
> payload is not).
>
> **Goal**: ship a single 256 KiB cap applied uniformly across
> every write tool, with a clean `body_too_large` `ToolError`
> that names the cap and the remediation.
>
> **Done when**: Layer-1 tests cover: a 256 KiB body (exactly
> the cap) succeeds (boundary, inclusive); a 257 KiB body
> raises the `body_too_large` ToolError on every write tool
> (one test per tool, parametrized; the cap is enforced at
> the tool-handler level, not the SB-client level); the error
> message includes both the body size and the cap (so the
> agent sees the numbers, not just a vague "too large"); the
> cap is NOT applied to `read_page`, `list_pages`,
> `page_exists`, `diff_pages`, `list_tasks`, or the
> journal-discovery tools. Layer-2 test via the real ASGI
> transport. Live-SB integration not needed (the cap is
> enforced locally; the failure surfaces before the SB
> round trip).
>
> **Files when resolved**:
> `src/mcp_silverbullet/server.py` (new `_check_body_size(body)`
> helper that raises the `ToolError`; called at the top of
> every write-tool handler — or, cleaner, a decorator that
> runs the check before the handler body),
> `tests/test_tools_in_memory.py` (parametrized Layer-1 cases
> across all write tools; one test confirms `read_page` and
> `list_pages` are unaffected).
>
> **Blocks on**: nothing — independent of T31 and of the
> journal gate. **Unblocks**: none directly.
>
> **Out of scope** (intentionally):
> - Configurable cap (`MCP_SILVERBULLET_BODY_SIZE_CAP`).
>   `xmatthewx`'s cap is fixed at 256 KiB; we mirror that.
>   Operators who need a different cap can fork.
> - The cap does NOT replace SB's own size limits. SB may
>   accept a smaller body than the cap; the bridge's cap is
>   a guardrail for the agent, not a promise about SB's
>   limits.
> - Streamed / chunked writes. The cap is the boundary that
>   tells an agent "stop trying to write 600 KB in one call;
>   use `append_to_page` chunks". A streamed / chunked write
>   tool is a separate design effort and a v1.4+ concern.

## Not yet specified

<!-- dim view of what's coming: things we suspect we'll ticket but
can't yet phrase precisely -->

- **`read_pages(names[])` / `write_pages(updates[])` batched
  primitives** — specifiable when an agent's actual workflow pays
  for N round trips and a measured client (Grok on the web)
  charges per call. `obsidian-mcp`'s `note_read_many` is the
  reference shape (bounded, with per-path skip reasons).
- **Soft-delete trash layer / `restore_page`** — v1's
  `delete_page` is hard delete; an agent that wants undo
  composes `read_page → write_page(<backup>) → delete_page`
  itself today. `xmatthewx`'s soft-delete-to-`_trash/` is the
  reference shape; it changes the contract (an `if_match`
  against a soft-deleted page should 404, `list_pages` should
  hide by default with `include_trash=True` opt-in) and adds
  API surface without an obvious agent need. Punt to v1.4.
- **Token-level / word-level diff** in `diff_pages` — v1.2
  ships line-based (T27); finer-grained diffing is a v1.4
  refinement.
- **Structured `{error, status, message, remediation}` error
  envelope** (`xmatthewx`-style) — the v1.2 surface uses
  `ToolError(message)` strings; a typed envelope would let
  agents pattern-match on `error: "conflict"` vs `error:
  "not_found"` instead of substring-matching on the message.
  Worth doing once we have one or two more error classes
  that benefit from machine-readable discrimination.
- **`OBSIDIAN_TOOLS`-style tool allow/deny profiles** —
  `obsidian-mcp` exposes
  `OBSIDIAN_TOOLS=core/read/minimal/!foo,bar`. Useful when
  the same MCP server is shared between an editor-side
  client (full tools) and a read-only web client (just
  `read_page` / `list_pages`). Our setup is
  single-client-per-deployment today; defer until we have a
  multi-client use case.
- **`obsidian-mcp`-style frontmatter helpers**
  (`get_frontmatter(name, key?)`, `set_frontmatter(name, key,
  value, if_match?)`, `merge_frontmatter(name, updates,
  if_match?)`) — YAML hand-roll is fragile; needs a real
  YAML dep or a more careful shape (a YAML dep would break
  the standing "no new deps" preference). Punt to v1.4+.
- **`obsidian-mcp`-style heading-/block-targeted patch**
  (`note_patch` with `target_type` ∈ {heading, block,
  frontmatter}) — more structured than our line-range patch
  but breaks the "no markdown AST" preference.
- **Pagination on `list_pages`** — today the entire space is
  returned in one chunk; a multi-thousand-page space makes the
  response multi-MB. Cursor-paged response is the right
  answer but needs a measured pain point.
- **Locking primitive** (`lock_page(name, ttl_s)` /
  `unlock_page(name)`) — protects an agent's edit window
  against a faster concurrent agent. Real concurrency
  primitive; different design effort. The v1.2 freshness
  invariant from `are/bmad-mcp-silverbullet` is the closest
  reference shape, but it composes differently than an
  HTTP-correct `If-Match` story.
- **Idempotency keys on writes** — today a retried call does
  two writes (the second fails 412 if the first landed,
  assuming T31 closes positively). A header that the bridge
  recognizes ("I already did this") would let it return the
  prior result without re-issuing. Protocol-level concern.
- **Per-page permissions (none / read / append / write)** —
  `are/bmad-mcp-silverbullet`'s core innovation, declared in
  plain markdown via `#mcp/config` blocks. Adds a trust
  model on top of SB that we don't need for a single-user
  bearer. Worth re-evaluating if we ever serve multiple
  agents.
- **SB Runtime API / Space Lua integration**
  (`are/bmad-mcp-silverbullet`-style) — bigger envelope,
  atomic lastModified from index (not filesystem), ability
  to run page-level queries server-side. Requires the
  Runtime API to be enabled (Chrome / `-runtime-api` Docker
  variant) and is tagged `#maturity/experimental` upstream.
  Tradeoff worth re-visiting if SB's Runtime API
  stabilizes.
- **Auto-migrate bullets to add wikilink refs** — destructive;
  the user didn't ask for it; explicitly out of scope for
  v1.2 (T29 / T30). Specifiable when a real "I want to
  retroactively address my old kanban bullets" workflow
  appears.

## Out of scope

<!-- Work ruled beyond this map's destination. Closed/fog items go in
"Decisions so far" or "Not yet specified" respectively; this section
is for *scope* boundaries. -->

- **`/healthz` endpoint** — operator-facing deploy probe. Was on
  the v1.1 chart's original T14, demoted when v1.1 redrew.
  v1.2 continued to punt. v1.3 continues to punt; not an
  agent-facing need.
- **`scopes_supported` in the discovery doc** — one-line
  `AuthSettings(required_scopes=[...])` change.
  Server-operator polish, not agent-facing. Punt.
- **`allowed_origins` env var** — browser-side MCP clients.
  Punt.
- **`json_response=True` mode** — non-streaming clients. Punt.
- **PR to nixpkgs upgrading `python3Packages.mcp` to v2.x** —
  same punt as the prior maps.
- **Server-pushed notifications / `subscriptions/listen`** —
  Punt.
- **OAuth 2.1, dynamic-client registration, multi-user.** Locked
  out at T2 of the v1 map; v1.3 inherits.
- **Semantic search / BM25 / embeddings.** Locked out at
  `docs/design.md` § Goals/non-goals; substring search (T34) is
  the v1.3 carve-out.
- **Re-deciding design questions locked in `docs/design.md`.**
  Same boundary as the prior maps.
- **Journal-surface write paths.** Read-only carries forward;
  the new v1.3 discovery tools (`search_pages`,
  `find_backlinks`) inherit the journal-read-only
  constraint.
- **A standalone trash layer / soft delete.** Same boundary as
  the v1.1 map; `delete_page` is hard delete.
- **Markdown-aware patch.** Line-range, find-and-replace, and
  the new `prepend_to_page` work on raw text; the bridge does
  not parse markdown ASTs.
- **Backlink rewrite on rename.** Same boundary as the v1.1
  T22 standing preference; `move_page` does NOT rewrite
  `[[backlinks]]` and v1.3 doesn't revisit this. T35
  (`find_backlinks`) helps an agent *find* the affected
  references, but the rewiring itself is still the agent's
  job.

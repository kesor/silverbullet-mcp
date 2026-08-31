<!--
Local-markdown tracker (v1.3's tracker lives in `map-v1.3.md`; this map
is the next effort). The v1.3 destination was "agent-grade discovery +
edit hygiene" and reached it on 2026-08-30 with eight tickets all
closed — T31 (negative), T31a / T31b (synthesized-etag fallback +
post-write verification), T32 / T33 (create_page / prepend_to_page),
T34 / T35 (search_pages / find_backlinks, both journal-gated), T36
(256 KiB body-size cap). v1.4 narrows the v1.3 charter's residual gap:
a bug filed against the v1.3 surface (the bridge name was masked as
`kesor_*`; the surface behavior matches v1.3's exactly) shows that
operators **without** the journal gate enabled have no usable way to
narrow page listings by content, because `list_pages` only filters by
`startswith(prefix)` — a parameter the bug reporter reasonably read as
"filtered by substring" given the description's wording. Two tickets:
T37 widens `list_pages`'s filter from `startswith` to substring
(`contains`), T38 surfaces the journal gate's purpose and opt-in path
more loudly in `docs/design.md`, the README, and the tool descriptions.

Standing preferences from the prior maps continue to apply unless
overridden here:

- Off-the-shelf libraries only — `mcp==2.1.1`, `httpx`, Starlette,
  uvicorn, pytest. No new Python deps.
- Side-car process; one bearer secret on both hops; no daemonization;
  no OAuth 2.1 dance.
- Every write tool honors `if_match`.
- Every write tool returns the T23 ack envelope.
- Layer 1 (in-memory `Client(mcp)`), Layer 2 (real ASGI transport),
  Layer 3 (`httpx.MockTransport` against `sb_client`) test split.
- The non-goals in `docs/design.md` § Goals/non-goals continue to
  bind: OAuth 2.1, multi-user, semantic search, mutating SB's source,
  hosting the bridge for other people are out of scope.

When in doubt, `docs/wayfinder/map.md` / `map-v1.1.md` /
`map-v1.2.md` / `map-v1.3.md` are the source of truth on standing
preferences; this map inherits them.
-->

# Wayfinder Map — `mcp-silverbullet` v1.4 (clarify the discovery surface)

## Destination

> **v1.4: clarify the discovery surface for operators without the
> journal gate.** Two tickets. The `list_pages` filter widens from
> `startswith(prefix)` to also accept a substring match, so an
> operator with only the HTTP surface can narrow the listing by a
> phrase inside a page name (e.g. `contains="GGLL"`) the way they
> reasonably expected the existing `prefix=` parameter to behave
> when they read the description. The journal gate's purpose and
> opt-in path surface more loudly: the README explains what
> `MCP_SILVERBULLET_JOURNAL_TOOLS=1` + `MCP_SILVERBULLET_SPACE_PATH`
> unlock (`pages_touching_topic` body-substring search, `search_pages`
> bounded wrapper, `find_backlinks` wikilink scan), and the tool
> descriptions point at the gate instead of leaving an operator to
> grep the source.

The shape:

- **Filter widening (T37)**: `list_pages(prefix="", contains="")`
  accepts both. `prefix=` retains `startswith` semantics (cheap, no
  scan cost beyond the existing `m.name.startswith(...)` line);
  `contains=` does substring matching against `m.name`. The two
  compose: a caller that passes both gets the AND of the two filters
  (a tighter list, never a wider one). The bug reporter's case
  (`contains="GGLL"`) now returns zero rows on the current SB the
  way `prefix="GGLL"` already does, but with the surface meaning
  matching the user's mental model. A user who actually has a
  `GGLL` page can find it.
- **Gate documentation (T38)**: the `list_pages` description
  explicitly notes that *body* search lives behind the journal
  gate (not the `list_pages` filter, which only ever matched
  against `name`); the README gets a "Discovery tools" section
  that walks through `MCP_SILVERBULLET_JOURNAL_TOOLS=1` +
  `MCP_SILVERBULLET_SPACE_PATH`, naming the three tools it
  unlocks. `docs/design.md` § Tools gains a "What we are not
  doing" line about substring body search without the gate:
  SB has no built-in search API, the bridge has no other way
  to read page bodies, so the gate is the only path.

### Status

Charted 2026-08-30 in response to the bug filed against
`kesor_list_pages` ("prefix silently ignored"). The bug's
first claim (`prefix=` does nothing) does **not** reproduce
against the current code: `server.py` line 1976-1977 does
`metas = [m for m in metas if m.name.startswith(prefix)]`
before hydration, and `tests/test_tools_in_memory.py::
test_list_pages_filters_by_prefix` locks the behavior. The
bug's second claim (no `search_*` tool) is also partly wrong
against the current code: `search_pages` shipped in v1.3
(T34), along with `pages_touching_topic` (T12) and
`find_backlinks` (T35) — all journal-gated. **But the
experience the bug describes is real for operators who
don't have the gate enabled**, and the `list_pages` filter's
`startswith`-only behavior is genuinely surprising for users
who read "filtered by prefix" as "substring." T37 + T38 close
that residual gap. **Status as of 2026-09-01**: T37 (commit
`205b1`) and T38 (this session) shipped; **v1.4
destination reached**. The bridge now exposes
`list_pages(prefix=, contains=)` for name narrowing,
the README's Discovery tools (journal-gated) section
front-loads the journal gate's purpose, and an
operator who hits a "no body search" dead end has an
obvious next step rather than relitigating the scope.

## Notes

- **Domain**: same as the prior maps (protocol bridge). v1.4
  stays inside the existing MCP-SB boundary — no new transports,
  no new auth hop, no new dependencies.
- **Skills every session should consult**: `mattpocock/skills@grilling`,
  `mattpocock/skills@domain-modeling`, `incremental-implementation`.
  The prior maps' standing preferences continue to bind.
- **Standing preferences for this effort** (continuing from
  the prior maps):
  - **No new Python dependencies.** Both tickets reuse
    what's already in `sb_client.py` / `server.py`. T37 is
    a one-line filter change plus a parameter; T38 is docs.
  - **Filter widening stays client-side.** T37 keeps the
    v1 / T10 design's "client-side filter" stance: the bridge
    fetches the directory listing once, applies both filters
    in Python, and returns the narrowed set. No SB-side
    Space-Lua, no server-side query, no new endpoint. This
    is non-negotiable: the bridge's threat model and the
    `docs/design.md` § What we are not doing entry on
    server-side search both lock it out.
  - **`prefix=` keeps `startswith` semantics, even after T37.**
    Renaming `prefix=` to `query=` or making it substring would
    be a wire-breaking change for any caller already using the
    v1 / v1.1 / v1.2 / v1.3 surface, and there's no reason to
    break it: a caller who wants substring now has `contains=`;
    a caller who wants `startswith` still has `prefix=`. Both
    parameters compose (AND), neither replaces the other.
  - **Journal gate stays as configured.** T38 surfaces the
    gate but doesn't relax it. The gate exists because
    body-substring search (`pages_touching_topic`) requires
    filesystem access to the SB space directory, which the
    HTTP `/.fs` API alone can't provide. An operator who
    doesn't have `MCP_SILVERBULLET_SPACE_PATH` cannot
    substring-search bodies; that's not a v1.4 limitation,
    it's a property of the SB API surface. The bug reporter
    asked for "a real Space Lua search endpoint"; that lives
    on SB's roadmap, not this bridge's.
  - **T39-style "rule it out of scope" lives in the map body,
    not as a ticket.** The bug reporter's second suggested fix
    ("implement a real Space Lua search endpoint") is a
    SilverBullet-server feature, not a bridge feature. The
    bridge can't ship it; T38's docs note the limit so a
    future reporter doesn't relitigate.

## Decisions so far

<!-- index only — one line per closed ticket, link to the
ticket's resolution below -->

- [Chart pass, 2026-08-30](#status): v1.4 destination named ("clarify the discovery surface for operators without the journal gate"); T37 (substring filter) and T38 (gate docs) charted with full detail below; the bug reporter's "implement Space Lua search" suggestion closed as out-of-scope (SilverBullet feature, not a bridge feature; the bridge has no path to it) and recorded under `## Out of scope`; T37 / T38 were on the frontier (both unblocked, neither claimed) at chart time.
- [T37 (2026-08-31)](#t37-widen-list_pages-filter-to-also-accept-substring-matching): new `contains: str = ""` parameter on `list_pages` (parallel to the v1 `prefix=` filter); substring matching against page name; AND-composes with `prefix=` when both are set; either empty is a no-op for that criterion; both empty returns the full listing; runs client-side *before* per-page hydration (same ordering invariant T28 locked for `prefix=`); wire surface unchanged for v1 / v1.1 / v1.2 / v1.3 callers (the new parameter is purely additive); 4 new Layer-1 cases in `tests/test_tools_in_memory.py`; `docs/design.md` § Tools row + `README.md` tool inventory updated; `CHANGELOG.md` v1.4 header corrected and `### Added` section gains a T37 entry; T38 unblocked (the gate docs can now reference the new `contains=` parameter as the example of a name-only filter). **Status as of 2026-08-31**: T37 shipped.
- [T38 (2026-09-01)](#t38-surface-the-journal-gates-purpose-and-opt-in-path-more-loudly): renamed README's `### Optional: journal surface` to `### Discovery tools (journal-gated)`; reframed the section around the three discovery tools (`pages_touching_topic` / `search_pages` / `find_backlinks`) with the two env vars that unlock them (`MCP_SILVERBULLET_SPACE_PATH` + `MCP_SILVERBULLET_JOURNAL_TOOLS=1`) named in the preamble; moved the three journal-analysis tools (`journal_histogram` / `tag_summary` / `recent_pages`) to a smaller sub-list at the bottom of the section so the discovery trio front-loads the section's purpose; updated `list_pages` tool description to match the charter's exact wording ("Body-content search lives behind the journal gate (`MCP_SILVERBULLET_JOURNAL_TOOLS=1` + `MCP_SILVERBULLET_SPACE_PATH`); this filter only ever matches against page names."); `docs/design.md` § What we are not doing (v1) gains a dedicated bullet on body-substring search without the gate (the SB-API surface is read-by-name only, no body index; the journal gate is the only path); the env-var table rows for both gate vars gain parenthetical pointers at the new section; the same pointer shows up in the `list_tasks` per-page-form description and in the Pi-session usage section so every "where do I find the journal tools" thread converges on the new heading; 1 new Layer-1 case in `tests/test_tools_in_memory.py` (`test_t38_list_pages_description_points_at_journal_gate`) pins the description's gate pointer so a future drift surfaces as a test failure rather than as a relitigation of the bug reporter's "no `search_*` tool" experience; `CHANGELOG.md` v1.4 status line updated from "T38 still on the frontier" to "T37 + T38 shipped; v1.4 destination reached"; v1.4 `### Added` section gains a T38 entry with the full threading list; **v1.4 destination reached**.

## Not yet specified

<!-- in-scope fog that can't be ticket-sized yet; graduates as
the frontier advances -->

- **Should `list_pages` carry an `exclude=` parameter?**
  Inverse filter (substring *not* in name) would let
  an operator skip a folder without typing the prefix
  for every page they want. Not in T37 / T38's
  charter; surfaces here as fog because someone reading
  T37 / T38 will ask. Stays as fog — the
  parameter would be additive (parallel to
  `contains=`) but no caller has actually needed it
  yet, so promoting it to a ticket would be premature.
  Future map if a real caller asks.

## Out of scope

<!-- scope boundaries, not steps on the route; never graduate -->

- [Bug reporter's "Space Lua search endpoint" suggestion](#suggestions-from-the-bug-report): ruled out as a bridge feature. SB's `server/src/handlers/fs.rs` exposes no search endpoint; Space Lua is an in-editor scripting layer, not an HTTP API. The bridge has no path to body-substring search without filesystem access. T38 documents the limit so a future reader doesn't relitigate.
- [Bug reporter's "N+1 pattern (list everything → grep → read each candidate)" workaround](#suggestions-from-the-bug-report): ruled out as a bridge feature, valid as an operator pattern. The bridge can't read page bodies without filesystem access; an operator who has that access turns on the journal gate and uses `pages_touching_topic` directly. An operator who doesn't is stuck with the list-then-grep dance; the bridge doesn't optimize that dance for HTTP-only operators because there's nothing to optimize (the bridge already returns the smallest possible list per `prefix`/`contains` filter).
- [Renaming `prefix=` to be substring](#suggestions-from-the-bug-report): wire-breaking change for any v1 / v1.1 / v1.2 / v1.3 caller; no benefit over adding `contains=`.

## Suggestions from the bug report

<!-- the bug report's two suggested fixes, scored; lives here
so a future session doesn't re-derive them -->

- **"Honor the documented client-side filter — fetch the full
  listing from SB (GET /.fs), apply prefix substring filtering in
  the bridge, and return only the matching rows."** Partially
  already done: the v1 code applies `startswith` filtering
  client-side (T10 fixed the v1-pre-T10 "no filter" regression).
  T37 widens `startswith` to also accept substring via
  `contains=`. The bug reporter's reading of "prefix" as
  "substring" was reasonable — T37 makes the surface match.
- **"Implement a real Space Lua search endpoint (kesor_search
  or a query argument on list_pages)."** Out of scope: SB has
  no HTTP search endpoint to wrap. The closest the bridge
  can get without filesystem access is `GET /.fs` +
  per-page `GET /.fs/{name}` (the existing N+1 pattern);
  the bridge already supports this via `list_pages` +
  `read_page`. With T37's `contains=` parameter, the
  list-side narrowing is cheap; the read-every-candidate
  dance remains for body-only matches, which is what the
  journal gate solves.

## Tickets

<!--
Each ticket is sized to one 100K-token session. Mark with
label `wayfinder:<type>`. Claim by setting an `Assignee:` line
at the top of the ticket's block (no real "assignee" field
exists in this local tracker; the line IS the claim —
concurrent sessions skip any ticket that already has one).
Tickets wire blocking edges in a second pass (the tracker
is a single file; "blocking" is rendered by ticket
ordering and an explicit "Blocks:" line per ticket).
-->

### T37. Widen `list_pages` filter to also accept substring matching

> **Labels**: `wayfinder:task`
> **Type**: AFK
> **Assignee**: pi (claimed + resolved 2026-08-31)
> **Status**: 🟢 closed — shipped 2026-08-31 (commit `205b1`)
> **Question**: How does `list_pages` accept substring matching
> alongside its existing `startswith` `prefix=` filter, without
> breaking the v1 / v1.1 / v1.2 / v1.3 wire surface?
>
> **Context**: The bug filed against `kesor_list_pages`
> (mirrored as `list_pages` in this repo) reported that
> `prefix="GGLL"` returns all 2,876 entries. Against the
> current code the symptom doesn't reproduce — the
> function does `m.name.startswith(prefix)` and would
> return 0 rows for `prefix="GGLL"` — but the *reading* of
> the description ("filtered by prefix") as substring is
> reasonable. The current surface forces an operator who
> wants substring to know that they need to enable the
> journal gate and use `pages_touching_topic`, which is
> heavier (full FS walk, body read per page) than they
> actually need for a name substring. A small,
> predictable surface widening: add a `contains=`
> parameter to `list_pages` that does substring matching
> against `m.name`, alongside the existing `prefix=`
> filter. The two compose (AND).
>
> **Goal**: `list_pages(prefix="", contains="")` accepts
> both. `prefix=` retains `startswith` semantics
> (unchanged from v1 / T10); `contains=` does substring
> matching against `m.name`. Both empty → no narrowing
> (the v1 behavior, returns the full listing). Both
> set → AND of the two filters. Neither ever expands
> the result set beyond the v1 default. The bug
> reporter's case (`contains="GGLL"`) returns 0 rows
> on the current SB; a user who actually has a `GGLL`
> page would find it via `contains="GGLL"`.
>
> **Done when**:
>
> - Layer-1 test (`test_list_pages_contains_filter`) mocks
>   a SB `GET /.fs` response with three pages
>   (`index`, `journal/2026-01-01`,
>   `trade-journal-2026-q1`) and asserts
>   `list_pages({"contains": "journal"})` returns the
>   two journal pages.
> - Layer-1 test (`test_list_pages_prefix_and_contains_compose`)
>   asserts the AND composition: `prefix="journal/"` +
>   `contains="2026"` returns only `journal/2026-01-01`
>   and `journal/2026-01-02` (not `trade-journal-…`).
> - Layer-1 test (`test_list_pages_contains_empty_is_full_list`)
>   asserts `contains=""` returns the full listing
>   (no narrowing), matching the v1 behavior.
> - Layer-1 test (`test_list_pages_contains_does_not_break_prefix`)
>   asserts the existing `prefix=` test case from
>   `test_list_pages_filters_by_prefix` still passes
>   unchanged (the wire surface for `prefix=` is not
>   altered).
> - The `list_pages` tool description is updated to
>   document both parameters and the AND composition.
> - `docs/design.md` § Tools row for `list_pages` is
>   updated to reflect the new shape.
> - Live-SB test (env-gated per v1 T7) is optional; the
>   Layer-1 coverage above is sufficient because the
>   change is a one-line filter widening with no
>   network behavior to verify.
>
> **Files when resolved**:
> `src/mcp_silverbullet/server.py` (one-line filter
> widening in `list_pages`), `tests/test_tools_in_memory.py`
> (Layer-1 cases), `docs/design.md` (§ Tools row),
> `docs/wayfinder/map-v1.4.md` (resolution entry).
>
> **Blocks on**: nothing — this is the lead ticket.
> **Unblocks**: T38 (gate docs reference the new
> `contains=` parameter).
>
> **Out of scope** (deliberately): changing `prefix=`
> to substring (would break every v1 / v1.1 / v1.2 /
> v1.3 caller); adding an `exclude=` parameter (see
> `## Not yet specified`); server-side Space Lua
> search (SB feature, not a bridge feature — see
> `## Out of scope`); renaming `prefix=` to a more
> general `query=` (no benefit over adding
> `contains=`; would break the wire surface).

**Resolution** (positive, 2026-08-31; commit
`205b1`): shipped in `src/mcp_silverbullet/server.py`
and `tests/test_tools_in_memory.py`. The change is
a one-line filter widening plus a parameter and a
description refresh — exactly the charter:

- New `contains: str = ""` parameter on
  `list_pages`. Substring matching against page
  name (parallel to `prefix=` which keeps v1's
  `startswith` semantics). The two compose as
  AND when both are set; either empty is a no-op
  for that criterion; both empty returns the full
  listing (v1 default). The new parameter slots
  in as the second positional/keyword argument
  on the tool, after `prefix=`, matching the
  existing pattern where each filter takes a
  default of `""` and applies only when non-empty.
- New filter line in the handler:
  ``if contains: metas = [m for m in metas if
  contains in m.name]`` — runs *after* the
  existing `prefix=` filter and *before*
  per-page hydration, mirroring the T28
  filter-before-hydrate ordering. A narrow
  `contains=` reduces the per-page round-trip
  count the same way `prefix=` does; a future
  refactor that re-orders the filters and
  hydration is caught by the same
  `test_list_pages_hydration_runs_after_prefix_filter`
  pin (T28) plus the new
  `test_list_pages_contains_runs_before_hydration`
  pin (T37).
- Tool description updated to advertise both
  parameters and the AND composition, plus a
  one-sentence note that body-content search
  lives behind the journal gate (T38's teaser —
  the full T38 work is a separate ticket; this
  sentence exists so an operator who reads the
  description post-T37 and tries to find a
  substring body has a clear pointer rather
  than a dead end). The description is
  targeted: the new parameters add a
  one-paragraph shape at the top, the rest
  of the v1.2 T28 hydration paragraph stays
  unchanged.
- No wire-shape change for v1 / v1.1 / v1.2 /
  v1.3 callers — the existing `prefix=` surface
  is byte-for-byte unchanged; the new
  `contains=` parameter is purely additive. A
  caller that already passes `prefix=` sees
  no behavior change.
- No new dependencies. No SDK version
  requirement change. No new env vars. The
  change is local to `list_pages` in
  `server.py`.

Four new Layer-1 cases in
`tests/test_tools_in_memory.py`:

1. `test_list_pages_contains_filter` — a
   caller passing `contains="journal"`
   against a list with `index` +
   `journal/2026-01-01` + `trade-journal-2026-q1`
   gets the two journal rows (substrings
   anywhere in the name match).
2. `test_list_pages_prefix_and_contains_compose` —
   `prefix="journal/"` + `contains="2026"` returns
   only the rows under the `journal/` folder that
   also contain `2026`. The `trade-journal-…` row
   matches `contains="journal"` but fails
   `prefix="journal/"`, so it's excluded.
3. `test_list_pages_contains_empty_is_full_list` —
   `contains=""` returns the full listing (no
   narrowing). Pins the empty-sentinel semantics
   so a future refactor that flips the
   empty-check doesn't silently drop rows.
4. `test_list_pages_contains_runs_before_hydration` —
   with hydration enabled and
   `contains="journal"`, only the two journal
   rows are visited for etag-hydration (the
   `index` row's etag is irrelevant because the
   filter discards it before the hydration
   walker runs). Pins the
   filter-before-hydrate ordering invariant
   so a future refactor that re-orders the
   pipeline surfaces as wasted SB load rather
   than a silent regression.

All 525 tests pass (521 pre-T37 + 4 new T37
cases). Live e2e tests skip cleanly without
env vars (gated on
`MCP_SILVERBULLET_LIVE_SB_URL` / `_TOKEN`).

Docs updated:

- `docs/design.md` § Tools row for `list_pages`
  updated: parameter column gains
  `contains: str = ""`; the row's Side-effects
  column gains a paragraph naming the AND
  composition, the empty-sentinel semantics,
  and the journal-gate scope boundary (so a
  reader of design.md who follows the row to
  understand the v1.4 surface sees T37's
  shape in one place).
- `README.md` tool inventory entry for
  `list_pages` updated: the function signature
  gains `(prefix?, contains?)`; the body of
  the entry explains both filters, the AND
  composition, and the journal-gate body-
  search scope. The "Discovery tools
  (journal-gated)" section's existing
  preamble is left alone (T38 will widen
  that section).
- `CHANGELOG.md` v1.4 `[v1.4]` header
  corrected: pre-T37 it said "T37 + T38
  shipped" (forward-looking); post-T37 it
  says "T37 shipped 2026-08-31; T38 still on
  the frontier". The v1.4 `### Added`
  section gains a T37 entry documenting
  the new `contains=` parameter and the
  AND-composition rule.

T38 (journal gate docs) is now the lead
frontier ticket on the v1.4 map. T37 was
the blocker per the ticket's `Unblocks:`
note; once T37 ships, T38 is unblocked and
can be claimed by the next session.

### T38. Surface the journal gate's purpose and opt-in path more loudly

> **Labels**: `wayfinder:task`
> **Type**: AFK
> **Assignee**: pi (claimed 2026-09-01)
> **Status**: 🟢 resolved (this session)
> **Question**: How does the README and the tool
> descriptions make the journal gate's purpose and
> opt-in path obvious to an operator who hasn't yet
> read `docs/design.md`?
>
> **Context**: The bug reporter couldn't find a
> `search_*` tool because they didn't have the journal
> gate enabled (`MCP_SILVERBULLET_JOURNAL_TOOLS=1` +
> `MCP_SILVERBULLET_SPACE_PATH`), and the README +
> tool descriptions don't surface that gate explicitly.
> An operator who hits `list_pages(contains="GGLL")`
> (post-T37) and still doesn't find the page (because
> "GGLL" is in a body, not a name) has no obvious
> next step. T38 makes the gate's purpose visible
> at the points of contact: the README's tool
> inventory gains a "Discovery tools (journal-gated)"
> section that names the three tools (`pages_touching_topic`,
> `search_pages`, `find_backlinks`) and the two env vars
> that unlock them; the `list_pages` description
> (post-T37) explicitly notes that *body* search lives
> behind the journal gate; `docs/design.md` § Tools
> gains a "What we are not doing" line about substring
> body search without the gate (so future readers don't
> relitigate).
>
> **Goal**: an operator who can't find a page reads
> the README, sees "Discovery tools (journal-gated)",
> follows the env vars, restarts the bridge, and the
> three tools appear. An operator who only ever wanted
> `list_pages` narrows isn't forced to read about the
> gate (it's a sidebar, not a wall).
>
> **Done when**:
>
> - README has a "Discovery tools (journal-gated)"
>   section under the existing tool inventory,
>   naming `pages_touching_topic`, `search_pages`,
>   `find_backlinks`, with a one-line summary of what
>   each does and the two env vars that gate them.
> - The `list_pages` tool description (post-T37)
>   gains one sentence: "Body-content search lives
>   behind the journal gate (`MCP_SILVERBULLET_JOURNAL_TOOLS=1`
>   + `MCP_SILVERBULLET_SPACE_PATH`); this filter
>   only ever matches against page names."
> - `docs/design.md` § Tools gains a "What we are
>   not doing" line that explicitly states: SB has
>   no HTTP search endpoint; body-substring search
>   requires filesystem access; the journal gate is
>   the only path. This locks in the scope boundary
>   so a future map doesn't re-litigate.
> - README's existing env-var section
>   (`MCP_SILVERBULLET_*`) gets a one-line note
>   next to `MCP_SILVERBULLET_JOURNAL_TOOLS` and
>   `MCP_SILVERBULLET_SPACE_PATH` pointing at the
>   new "Discovery tools" section.
>
> **Files when resolved**:
> `README.md` (new "Discovery tools" section +
> env-var note), `src/mcp_silverbullet/server.py`
> (one-sentence addition to `list_pages`'s
> description), `docs/design.md` (§ Tools
> "What we are not doing" line), `docs/wayfinder/
> map-v1.4.md` (resolution entry).
>
> **Blocks on**: nothing — but reads cleaner after
> T37 ships so the description can reference both
> `prefix=` and `contains=`. Can claim either order.
> **Unblocks**: nothing — terminal ticket.
>
> **Out of scope** (deliberately): relaxing the
> journal gate (the gate exists for a real reason —
> see `## Notes`); adding a body-substring search
> tool that works without the gate (impossible at
> the bridge layer); rewriting the README
> end-to-end (T38 is a targeted addition, not a
> doc rewrite).

**Resolution** (positive, 2026-09-01; commit
pending — this session): shipped in
`README.md`, `src/mcp_silverbullet/server.py`,
`docs/design.md`, `tests/test_tools_in_memory.py`,
and `CHANGELOG.md`. Implementation matched the
charter with one scope-shape decision and one
drive-by widening:

- **README's `### Optional: journal surface`
  section is renamed to `### Discovery tools
  (journal-gated)`** and reframed around the
  three discovery tools (`pages_touching_topic`,
  `search_pages`, `find_backlinks`). The section's
  preamble now names the two env vars that
  unlock the gate (`MCP_SILVERBULLET_SPACE_PATH` +
  `MCP_SILVERBULLET_JOURNAL_TOOLS=1`) with a
  one-line summary of each, framed in the bug
  reporter's language ("when `list_pages(prefix=,
  contains=)` narrows the listing but the page
  you want isn't on a name match — its name
  doesn't contain the phrase, but its *body*
  does — the HTTP `/.fs` API can't help: SB has
  no built-in search endpoint, so substring search
  over page bodies needs filesystem access to the
  SB space directory"). The three journal-analysis
  tools (`journal_histogram` / `tag_summary` /
  `recent_pages`) move to a smaller sub-list at
  the bottom of the section under "Three additional
  journal tools (also gated, but not
  discovery-flavoured)" so the discovery trio
  front-loads the section's purpose without
  dropping the other three tools (they're still
  journal-gated and still surface here).

- **The `list_pages` tool description matches the
  charter exactly.** Pre-T38 the description
  ended with a forward-looking "T38 — see the
  README's Discovery tools section" parenthetical
  (T37's commit added this as a placeholder).
  Post-T38 the parenthetical becomes a stable
  cross-reference to the README's new section
  by name: "this filter only ever matches against
  page *names*; body-content search lives behind
  the journal gate (`MCP_SILVERBULLET_JOURNAL_TOOLS=1`
  + `MCP_SILVERBULLET_SPACE_PATH`; see the
  README's Discovery tools (journal-gated)
  section)." The `T38 —` ticket-reference drops
  — the destination now exists, so the description
  doesn't need a forward-looking pointer anymore.
  Worded as a one-paragraph note at the end of
  the description so an agent reading the
  description sees the gate's existence *before*
  making the call, rather than after the call
  fails.

- **`docs/design.md` § What we are not doing (v1)
  gains a dedicated bullet on body-substring
  search without the gate.** Worded to lock the
  scope boundary so a future map doesn't
  re-litigate: "SilverBullet exposes no HTTP
  search endpoint (the `/.fs` API is read-by-name
  and list-by-directory, with no body-content
  index); the bridge has no way to substring-search
  page bodies without filesystem access to the SB
  space directory. The journal gate
  (`MCP_SILVERBULLET_JOURNAL_TOOLS=1` +
  `MCP_SILVERBULLET_SPACE_PATH`) is the only path.
  Operators without that gate can still narrow by
  name via `list_pages` (`prefix=` / `contains=`,
  T37); operators who want body search enable the
  gate and use `pages_touching_topic` /
  `search_pages`. T38 surfaces the gate more loudly
  in the README and `list_pages`'s description so
  an operator who hits a "no body search" dead end
  has an obvious next step rather than
  relitigating the scope." The § Tools row for
  `list_pages` gains a T38 reference and the
  parenthetical updates to match the README's
  new section name exactly ("Discovery tools
  (journal-gated) section") so an operator
  following the design.md cross-link lands on
  the right README anchor.

- **Env-var table rows gain parenthetical
  pointers.** `MCP_SILVERBULLET_SPACE_PATH` and
  `MCP_SILVERBULLET_JOURNAL_TOOLS` rows in the
  README's env-var table both append "(see
  [Discovery tools
  (journal-gated)](#discovery-tools-journal-gated))"
  so an operator who reaches the table from the
  boot-order section sees the gate's destination
  in the same sentence. The
  `list_tasks` per-page-form description and the
  Pi-session usage section also gain the same
  cross-reference so every "where do I find the
  journal tools" thread converges on the new
  heading.

- **One new Layer-1 case in
  `tests/test_tools_in_memory.py`:
  `test_t38_list_pages_description_points_at_journal_gate`** —
  builds an in-memory MCP server via the
  existing `_build` helper, calls `list_tools`,
  finds the `list_pages` entry, and asserts the
  description carries all four T38 tokens:
  `"matches against page *names*"` (the
  filter-scope note), `"body-content search"`
  (the missing-axis note), `"MCP_SILVERBULLET_JOURNAL_TOOLS=1"`
  (the opt-in env var by name), and
  `"MCP_SILVERBULLET_SPACE_PATH"` (the
  space-path env var by name). Pins the
  description so a future edit can't silently
  drop the pointer — the consequence of dropping
  it is exactly the bug reporter's experience (no
  obvious next step after a `list_pages` miss).
  The test name follows the existing
  `test_tNN_*` convention so it shows up
  alongside T37's substring-filter cases in the
  `grep -n "t3[78]"` output a future session
  will run to look up v1.4's surface.

- **`CHANGELOG.md` v1.4 status line and `###
  Added` section updated.** Status flips from
  "T37 shipped 2026-08-31; T38 still on the
  frontier" to "T37 + T38 shipped 2026-08-31;
  v1.4 destination reached." The T37 entry's
  closing line ("T38 (journal gate docs) remains
  on the frontier; this entry ships T37 alone.")
  is removed; a new T38 entry follows the T37
  entry in `### Added` documenting the README
  rename + refactor, the `list_pages` description
  update, the design.md `## What we are not doing`
  bullet, the env-var table pointers, and the
  Layer-1 test by name.

**Drive-by deviation from the ticket's charter**:
the charter specified naming the three discovery
tools + "a one-line summary of what each does and
the two env vars that gate them" in the README's
new section. The implementation names the tools
(the discovery trio front-loads the section's
purpose) but also keeps the three journal-analysis
tools (`journal_histogram` / `tag_summary` /
`recent_pages`) under the same section rather
than splitting them into a separate "journal
analysis" section. Rationale: all six tools are
gated by the same env-var pair, so splitting them
would force an operator who only reads the
discovery-tools intro to bounce to a second
section to discover the analysis tools exist. The
charter's "three tools + one-line summary" rule
is honored for the discovery trio (which gets
the prominent placement); the analysis trio is
moved to a smaller sub-list at the bottom of
the same section so they're discoverable from
the same heading. This is a presentation-shape
call, not a functional one — every tool the
charter names is present with the same
description.

All 526 tests pass (525 pre-T38 + 1 new T38
case); live e2e tests skip cleanly without env
vars (gated on `MCP_SILVERBULLET_LIVE_SB_URL` /
`_TOKEN`). No new dependencies. No SDK version
requirement change. No behavior change. No
wire-shape change. The bridge is byte-for-byte
the same on the success path and on the error
path; T38 is local to the README, the
`list_pages` description, the design.md
`## What we are not doing` block, the env-var
table rows, and the `list_tasks` /
Pi-session sections.

**Unblocks**: nothing (terminal ticket). **v1.4
destination reached** — all open tickets on
the v1.4 map (T37 + T38) shipped.

---

## Drive-by

<!-- findings from the charting session that don't belong
to a v1.4 ticket; recorded so a future session sees them -->

- **The bug report's first claim doesn't reproduce against
  the current code.** `server.py` line 1976-1977 does
  `metas = [m for m in metas if m.name.startswith(prefix)]`
  before hydration, and `tests/test_tools_in_memory.py::
  test_list_pages_filters_by_prefix` locks the behavior.
  The reporter's symptom (all 2,876 entries returned
  regardless of `prefix`) matches the v1-pre-T10 code, not
  what ships now. T37 doesn't fix a regression — it widens
  the existing filter so substring users don't have to
  guess.
- **The bug report's second claim (no `search_*` tool) is
  partly wrong.** `search_pages` shipped in v1.3 (T34),
  along with `pages_touching_topic` (T12) and
  `find_backlinks` (T35) — all journal-gated. The
  reporter couldn't see them because they didn't have
  the gate enabled. T38 makes the gate's purpose and
  opt-in path more obvious.
- **The v1.3 map's `## Drive-by` block already records
  two test-maintenance findings** (broken
  `Settings(...)` call and `patch_page_lines` byte-
  count drift). These pre-date v1.4 and are not in
  scope here; carrying forward as "known backlog."
- **The `kesor_*` tool naming in the bug report is
  not this repo's bridge name** — this repo's MCP
  exposes tools as `list_pages`, `read_page`, etc.
  The reporter is hitting a different MCP wrapping
  this bridge (likely a workspace-local renamer or
  a separate downstream consumer). T37 / T38 don't
  need to know about that — the surface they widen
  / document is the underlying `list_pages` /
  `read_page` / etc. contract, regardless of the
  consumer-facing name.

## Drive-by from this session (T38 resolution)

<!-- findings surfaced during T38's resolution that don't
belong to a v1.4 ticket; recorded so a future session sees
them -->

- **The `list_pages` description already carried a
  forward-looking `(T38 — …)` parenthetical** when T37
  shipped. T38 closed it by replacing the
  forward-looking ticket reference with a stable
  README-section cross-reference ("Discovery tools
  (journal-gated) section"). A future caller that
  introspects the description and pattern-matches on
  `T38` would have hit the parenthetical as dead text
  after T38 lands. The replacement keeps the same
  intent (gate pointer + discovery destination) and
  drops the ticket reference. No behavior change for
  agents that read the description semantically; the
  four tokens the Layer-1 test pins
  (`"matches against page *names*"` /
  `"body-content search"` /
  `"MCP_SILVERBULLET_JOURNAL_TOOLS=1"` /
  `"MCP_SILVERBULLET_SPACE_PATH"`) are all preserved.
- **The v1.3 map's T35 resolution entry references
  `§ Optional: journal surface`** as the section
  `find_backlinks` shipped under. This is locked
  historical record (the T35 commit predates the
  v1.4 T38 rename) and the v1.3 map's resolution
  block describes what shipped at T35 time, not what
  the section is called today. Not rewritten — the
  resolution is the v1.3 truth. A future map that
  needs to audit the rename's downstream history can
  find this in the v1.3 map's resolution block.
- **The v1.4 map's `## Not yet specified` section had
  three fog patches** (parameter name, description
  shape, AND-composition knob) that all resolved
  with T37's commit `205b1`. T38's resolution cleans
  them up so the section only carries the
  `exclude=` fog patch (which is genuinely still
  fog — no caller has needed it yet, so promoting
  it to a ticket would be premature).
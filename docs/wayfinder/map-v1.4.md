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
that residual gap. Charting only; resolution belongs to a
later session.

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

- [Chart pass, 2026-08-30](#status): v1.4 destination named ("clarify the discovery surface for operators without the journal gate"); T37 (substring filter) and T38 (gate docs) charted with full detail below; the bug reporter's "implement Space Lua search" suggestion closed as out-of-scope (SilverBullet feature, not a bridge feature; the bridge has no path to it) and recorded under `## Out of scope`; T37 / T38 are now on the frontier (both unblocked, neither claimed).

## Not yet specified

<!-- in-scope fog that can't be ticket-sized yet; graduates as
the frontier advances -->

- **Naming the new parameter.** `contains=` is the obvious
  choice (parallel to `prefix=`, reads as substring-or-better)
  but alternatives like `match=`, `name_contains=`, or
  collapsing into a single `query=` knob with a `mode=`
  argument are equally sharp. T37 will pick one; no
  pre-decision here.

- **Should the `list_pages` description advertise the
  composed behavior?** Two filters that AND together is
  slightly more cognitive load than one filter with a
  knob. The description is already long; an operator
  who only ever uses `prefix=` shouldn't have to read
  about `contains=` to use the tool. T37 will decide
  the description shape; pre-decision not sharp enough
  to ticket.

- **Should `list_pages` carry an `exclude=` parameter?**
  Inverse filter (substring *not* in name) would let
  an operator skip a folder without typing the prefix
  for every page they want. Not in T37's charter; surfaces
  here as fog because someone reading T37 will ask.

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
> **Assignee**: pi (claimed 2026-08-31)
> **Status**: 🟡 open — claimed, working
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

### T38. Surface the journal gate's purpose and opt-in path more loudly

> **Labels**: `wayfinder:task`
> **Type**: AFK
> **Assignee**: *(unclaimed)*
> **Status**: 🟡 open — unblocked, on the frontier
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
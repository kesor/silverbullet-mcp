<!--
Local-markdown tracker (v1.4's tracker lives in `map-v1.4.md`; this map
is the next effort). The v1.4 destination was "clarify the discovery
surface for operators without the journal gate" and reached it when
T37 + T38 charted on 2026-08-30; both are open, unclaimed, on the
frontier. v1.5 widens the agent-experience surface: the bugs filed
against the bridge in this session (and the prior bug filed under
the `kesor_*` alias) surfaced a recurring shape — agent callers
trip on input shape (`.md` suffix, empty inputs), and the bridge's
errors don't always help them recover. Three new tickets: T39
(name normalization for `.md` suffix), T40 (lift upfront empty-input
validation across the write tools), T41 (a doc-clarifications
batch for `index.md` source-vs-render + `move_page` no-op + the
`tool` descriptions pointing at the existing input-validation
guards). T42 (412 contention hints) is a small UX-polish follow-up
that surfaces when a caller is hitting the same page's `If-Match`
precondition over and over — a `concurrent_edit_hint: true` signal
in the error envelope so agents back off.

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
  bind: OAuth 2.1, multi-user, semantic search, mutating SB's
  source, hosting the bridge for other people are out of scope.

When in doubt, `docs/wayfinder/map.md` / `map-v1.1.md` /
`map-v1.2.md` / `map-v1.3.md` / `map-v1.4.md` are the source of
truth on standing preferences; this map inherits them.
-->

# Wayfinder Map — `mcp-silverbullet` v1.5 (agent-experience hardening)

## Destination

> **v1.5: agent-experience hardening.** Four tickets. The
> `.md`-suffix split (b1, b6) closes via name normalization on
> every `name`-taking tool (T39): callers pass `Foo`, the bridge
> resolves to `Foo.md` before any SB round trip, and the agent
> sees the body it expected. The empty-input 500s (b9) close via
> upfront validation lifted across the remaining write tools
> (T40): `write_page` gets the same `name must not be empty` /
> `content must not be empty` guards `create_page` /
> `append_to_page` / `prepend_to_page` already ship; `move_page`
> gets the empty-name guard; `delete_page` gets the empty-name
> guard; `patch_page_lines` and `patch_page_replace` get the
> empty-name guard. Three doc-only clarifications (T41) land in
> the same release: `read_page`'s description gains a one-line
> note that pages with `${template.each(...)}` content return
> raw markdown, never the rendered result (b5); `move_page`'s
> description gains a one-line note that `name == new_name` is a
> no-op that ignores `if_match` (b8); the bridge's overall
> `MCPServer.instructions` block gains a single sentence about
> the `.md`-suffix convention now that T39 normalizes it. The
> 412 contention hint (b10) ships as T42: after N=3 consecutive
> 412s on the same page within M=60 seconds, the bridge adds a
> `concurrent_edit_hint: true` field to the standard
> `ToolError("precondition failed; ...")`, so an agent in a
> contention window gets a clear signal to back off rather than
> pattern-matching `precondition failed` strings.

The shape:

- **Name normalization (T39)**: a single
  `_normalize_page_name(name) -> str` helper in
  `server.py`, threaded into every `name`-taking tool at the
  top of each handler (before the SB round trip, before
  `_check_body_size`). The helper applies the rule:
  strip leading/trailing whitespace; if the result has no
  `.` in its basename, append `.md`. Pass-through for any
  name that already has an extension (`.txt`, `.json`, etc.)
  so non-markdown files don't get a spurious suffix. The
  helper is idempotent (calling it twice yields the same
  value) and surfaces no error — the only tool that gets
  to reject an empty name is the upfront empty-input guard
  (T40), which runs *before* the normalization so a caller
  passing `name=""` still sees the loud empty-name error
  rather than the normalized form `".md"`.
- **Upfront input validation (T40)**: extend the existing
  pattern (`create_page` empty name, `append_to_page`
  empty text, `prepend_to_page` empty content,
  `patch_page_replace` empty find, `check_task` empty ref)
  to the four tools that don't have it yet: `write_page`
  (empty name + empty content), `move_page` (empty
  `new_name`), `delete_page` (empty name — same guard,
  reuse T40's helper), `patch_page_lines` (empty name),
  `patch_page_replace` (also: empty `new_string`). One
  `_validate_nonempty_name(name)` + `_validate_nonempty_*
  (value, label)` pair at module scope, threaded into each
  handler.
- **Doc clarifications (T41)**: targeted additions to
  three tool descriptions + one `MCPServer.instructions`
  sentence. No new code beyond the description strings.
- **412 contention hint (T42)**: a thin rate-limiter
  keyed on `(name,)` with a 60-second sliding window
  counting 412s on writes that threaded an explicit
  `if_match=<etag>`. The counter is *advisory* — the
  bridge still raises the standard 412 `ToolError`
  regardless — but the *envelope* gains a
  `concurrent_edit_hint: bool` field (default `False`,
  `True` after the threshold trips). The hint is a
  one-line addition to the MCP error payload shape;
  agents that don't check the field see no change. A
  test that issues four 412s in fast succession on the
  same page asserts the fourth one carries the hint.

### Status

Charted 2026-08-30 in response to three findings from this
session:

- The bug report filed under the `kesor_*` alias (the
  one that opened this session): the `list_pages`
  prefix-filter claim doesn't reproduce against the
  current code (the v1-pre-T10 bug was fixed at T10),
  and `search_pages` / `pages_touching_topic` /
  `find_backlinks` ship on this build as journal-gated
  tools. The bug reporter's *experience* (no body search
  for operators without the gate) is real and addressed
  by v1.4's T38 gate docs.
- The user's two observations from the same session:
  pydantic URLs leaking into errors (not reproducible
  against this codebase — see `## Drive-by`), and the
  `.md`-suffix habit tripping agents (real, addressed
  by T39).
- The b1-b10 list filed by an agent session against the
  bridge (the user pasted it after the chart): the real
  ones shape T39 / T40 / T41 / T42 below; the false-
  positive ones are recorded in `## Drive-by` so a
  future session doesn't relitigate.

The map has **four open tickets** (T39, T40, T41, T42),
all unblocked, all on the frontier. None claimed this
session — the user asked for a chart-only pass, not a
resolution.

## Notes

- **Domain**: same as the prior maps (protocol bridge).
  v1.5 stays inside the existing MCP-SB boundary — no
  new transports, no new auth hop, no new dependencies.
- **Skills every session should consult**:
  `mattpocock/skills@grilling`,
  `mattpocock/skills@domain-modeling`,
  `incremental-implementation`,
  `security-and-hardening`. The prior maps' standing
  preferences continue to bind.
- **Standing preferences for this effort** (continuing
  from the prior maps):
  - **No new Python dependencies.** All four tickets
    reuse what's already in `sb_client.py` / `server.py`
    / `journal.py`. T39 is one helper + threading; T40
    is one helper pair + threading; T41 is strings; T42
    is one `collections.deque`-backed counter + threading.
  - **T39's normalization helper is idempotent and
    side-effect-free.** A caller that passes `Foo.md`
    gets `Foo.md` back; a caller that passes `Foo` gets
    `Foo.md`; a caller that passes `Foo.txt` gets
    `Foo.txt` (no spurious `.md` append). The helper
    has no log output, no metrics, no observable side
    effect — it's a pure function the way the
    `_normalize_link_target` (T35) helper is a pure
    function. This matters because it's threaded into
    every tool: an agent that introspects its own
    `if_match` round-trip (read `.etag`, write `if_match`
    = `.etag`) is unaffected; an agent that reads the
    returned `PageMeta.name` after a normalization sees
    the *normalized* form, which is also what every
    subsequent tool call would resolve to — so the
    observable contract is consistent.
  - **T39's name normalization runs *after* T40's empty
    input guard, not before.** A caller passing
    `name=""` should still see `ToolError("name must
    not be empty")`, not the normalized form `".md"`
    silently succeeding. Order matters: empty guards
    fire on the caller's raw input; normalization fires
    on the validated input.
  - **T40's pattern is "lift the existing guard, don't
    invent a new shape."** Every guard added in T40
    uses the exact wording and shape of an existing
    guard on another tool: `name must not be empty` /
    `text must not be empty` / `content must not be
    empty` / `find must not be empty` / `new_string
    must not be empty` / `ref must not be empty`. The
    two helpers (`_validate_nonempty_name`,
    `_validate_nonempty_value`) are the only new code
    surface; the rest is mechanical threading.
  - **T42's contention hint is advisory, never
    authoritative.** The bridge still raises the
    standard 412 `ToolError` regardless of whether the
    hint fires. The hint is a `concurrent_edit_hint:
    bool` field on the error envelope that an agent
    *can* check to back off — a future caller that
    doesn't check the field sees no change. The hint
    doesn't change the wire shape of the success path;
    it only adds an optional field to the error
    envelope that MCP clients can ignore.
  - **T42's threshold is small and tunable.** N=3
    consecutive 412s in M=60 seconds is a starting
    point; a future caller that sees too many
    false-positives (the threshold is too aggressive)
    or too few (the threshold is too lax) can tune it
    via env vars or a constant edit. The constants are
    at module scope so the next session can adjust
    without a code-restructuring ticket.
  - **T39 is behavior-changing for v1 / v1.1 / v1.2 /
    v1.3 callers.** Any caller that explicitly passes
    `name="Foo"` expecting a 500-shaped error will
    instead get a 200 with the body of `Foo.md`. The
    doc string notes this. The risk is small (the
    caller was already getting an error and almost
    certainly retrying with `.md`), and the benefit is
    a unified surface — but it's worth calling out
    in the ticket resolution so the CHANGELOG entry
    is loud about it.

## Decisions so far

<!-- index only — one line per closed ticket, link to the
ticket's resolution below -->

- [Chart pass, 2026-08-30](#status): v1.5 destination named ("agent-experience hardening"); T39 (name normalization for `.md` suffix), T40 (upfront empty-input validation across the remaining write tools), T41 (doc clarifications — index.md source-vs-render, move_page no-op, `.md`-suffix convention note), T42 (412 contention hint) charted with full detail below; T43 (CF 5xx `cf_hint` envelope — b11, the user's 2026-08-31 CF 502 incident) charted in this session and is on the frontier; T44 (fix T31b's false-positive "concurrent edit detected" — b12, the user's 2026-08-31 patch_page_replace-lies-about-412 report) charted in this session and is on the frontier; b2 / b3 / the pydantic-URL observation from the prior turn closed as out-of-scope (the bridge's current code doesn't reproduce those claims) and recorded under `## Drive-by`; v1.4's T37 / T38 are still on the v1.4 frontier (this map doesn't re-litigate them).
- [T39 (2026-08-31)](#t39-normalize-name-inputs-to-handle-the-md-suffix-split-b1-b6): option A (auto-append `.md`) + a `name_resolution` envelope field that teaches the agent the convention for its next call; helper `_normalize_page_name(name)` at module scope in `server.py` (pure, idempotent, strips whitespace, appends `.md` to bare basenames); threaded into 12 call sites across 10 tools + the resource template + the hydration walker; error wording references the *resolved* name (consistent with the success-path envelope); behavior-changing for any v1 / v1.1 / v1.2 / v1.3 caller that explicitly passes a bare `name` and expected a 500-shaped error.
- [T39 (2026-08-31)](#t39-normalize-name-inputs-to-handle-the-md-suffix-split-b1-b6): option A (auto-append `.md`) + a `name_resolution` envelope field that teaches the agent the convention for its next call; helper `_normalize_page_name(name)` at module scope in `server.py` (pure, idempotent, strips whitespace, appends `.md` to bare basenames); threaded into 12 call sites across 10 tools + the resource template + the hydration walker; error wording references the *resolved* name (consistent with the success-path envelope); behavior-changing for any v1 / v1.1 / v1.2 / v1.3 caller that explicitly passes a bare `name` and expected a 500-shaped error.

## Not yet specified

<!-- in-scope fog that can't be ticket-sized yet; graduates as
the frontier advances -->

- **T42's threshold values (N, M).** N=3 / M=60s is a
  starting point; whether that matches the live
  contention pattern is empirical. The constants are
  at module scope, so tuning is a one-line change, not
  a ticket. Worth noting in T42's resolution that
  future tuning is expected.

- **Whether the `MCPServer.instructions` block (the
  system-prompt-ish text the agent sees on connect)
  should be updated independently of T41.** T41 adds
  three sentences; an `instructions`-block update is a
  bigger lift (the existing block is 50+ lines and
  locked at v1). Probably out-of-scope for v1.5; can
  wait for v1.6 or a T41a follow-up if the agent
  experience is still rough after T41 lands.

## Out of scope

<!-- scope boundaries, not steps on the route; never graduate -->

- [Bug reporter's b2 (etag hydration env var not working)](#drive-by-from-this-session): ruled out as a bridge fix. The env var `MCP_SILVERBULLET_LIST_PAGES_HYDRATE_ETAGS` is wired through `main.py` → `build_mcp` → `list_pages`'s handler (verified at `main.py:227-228`, `server.py:962`, `server.py:1979`). The reporter's symptom (etag=null on every row) is consistent with the documented behavior on this SB build: T31's negative finding established that SB's PUT responses strip `ETag` on this dev box, and the T31a synthesized-etag fallback applies on PUT but **not** on the `GET /.fs` (list) payload — the list endpoint is documented to omit `etag` on this build per T28. The reporter's claim is consistent with one of three states: (a) they're on a build before T31a shipped, (b) the env var isn't being passed to their MCP wrapper, (c) they're sampling rows that genuinely have no synthesized etag (which happens if `X-Last-Modified` is also stripped). None of these is a bridge bug; all are addressed by v1.4's T38 gate docs (which point at the journal tools for richer discovery).
- [Bug reporter's b3 (`read_page` passes upstream 500s as raw text on >3KB pages)](#drive-by-from-this-session): ruled out as a bridge fix. The bridge wraps every 5xx in `ToolError("silverbullet error: <status>")` per `docs/design.md` § Tools § Status-code mapping (verified at `sb_client.py:838, 841` for the `ServerError` raise and `server.py:198` for the `ToolError` translation). If a 500 from SB on a >3KB page is reaching the agent as raw text, the path is `read_page → _translate_sb_errors → ToolError(str(ServerError("silverbullet error: 500")))` — the resulting message is exactly `ToolError("silverbullet error: 500")`. The reporter's claim that it's "the upstream's wording, not the bridge's" doesn't match the current code. Possible explanations: (a) the reporter's MCP wrapper is unwrapping `ToolError` and showing the underlying `ServerError`'s body, (b) SB itself is returning a 500 with a body that contains a URL the agent is then mis-parsing as the bridge's wording. Either way, not a bridge code bug; recorded for future sessions.
- [User's prior-turn observation about pydantic URLs in errors](#drive-by-from-this-session): ruled out as a bridge fix. The bridge doesn't import pydantic anywhere except transitively through the MCP SDK. The bridge raises its own typed exceptions (`PageNotFound`, `ServerError`, `PreconditionFailed`, `BodyTooLarge`) and translates them to `ToolError` with concise wording (`server.py:192-200`). There's no path by which a pydantic URL reaches an error message from this codebase. If the reporter sees pydantic URLs in their error stream, it's coming from somewhere upstream — either their MCP wrapper, an LLM in the loop parsing it, or a different bridge entirely (the `kesor_*` naming suggests a different consumer, possibly with its own error-translation layer that pulls from pydantic). Recorded for future sessions.
- [Bug reporter's b7 (`patch_page_replace` empty-find upfront guard)](#drive-by-from-this-session): not a bug — the reporter marks it as "the right pattern." Listed here for completeness because T40 lifts this exact pattern to other tools.
- [Bug reporter's b8 (`move_page` `name == new_name` no-op)](#drive-by-from-this-session): not a bug — works as documented. The doc-only clarification lives in T41.
- [User's 2026-08-31 CF 502 wrapper-side leak](#status): ruled out as a bridge fix for the *wrapper-side* half. The wrapper's `Failed to refresh kesor: Error POSTing to endpoint: <full CF JSON>` surfaced the full CF 502 body because the wrapper was making its own POST against `sb.kesor.net` and CF returned 502 directly — the bridge wasn't in that path. The wrapper fix lives at the wrapper layer (truncate the body, surface a clean error code). The *bridge-side* defense against the same pattern (when the bridge's outbound SB call hits a CF-fronted SB that 502s) is T43.

## Drive-by from this session

<!-- findings from the chart session that don't belong to a
v1.5 ticket; recorded so a future session sees them -->

- **The original bug report's `list_pages` claim doesn't
  reproduce against the current code.** Verified at
  `server.py:1976-1977`: `metas = [m for m in metas if
  m.name.startswith(prefix)]` runs before hydration, with
  the regression test `test_list_pages_filters_by_prefix`
  locking the behavior. The reporter's 2,876-entry
  symptom matches the v1-pre-T10 code, not what ships
  now. v1.4's T37 widens the filter to also accept
  substring (`contains=`) so the bug reporter's mental
  model — "filtered by prefix" reads as substring —
  matches the surface.
- **The b-list's b2 (etag hydration) doesn't reproduce
  against the current code.** Verified the env var
  plumbing (`main.py:227-228` →
  `server.py:962` → `list_pages` handler at line 1979)
  and the synthesized-etag path through T31a (the
  `_etag_from_response` helper at `sb_client.py:630-682`).
  The reported symptom (etag=null on every row) is the
  documented behavior on this SB build per T28 + T31.
  v1.4's T38 documents the gate clearly so a future
  reporter doesn't relitigate.
- **The b-list's b3 (500 pass-through on >3KB pages)
  doesn't reproduce against the current code.** Verified
  the `ServerError("silverbullet error: {status}")` path
  at `sb_client.py:838, 841` and the `ToolError(str(exc))`
  translation at `server.py:198`. The bridge's wording
  is short and consistent — the only way the upstream
  text could reach the agent is if a downstream wrapper
  is unwrapping the `ToolError` and showing the underlying
  exception's `str()`, OR SB itself is including extra
  text in the 500 body that the agent is then parsing as
  a separate error. Either way, not a bridge code bug.
- **The pydantic-URL observation doesn't reproduce
  against the current code.** Verified the bridge has
  no pydantic imports outside the MCP SDK's transitive
  dependency, and the bridge's `ToolError` messages are
  short static strings (no dynamic content from the SB
  response body — `ServerError` carries only the status
  code, not the body). If a pydantic URL is reaching
  the agent, it's coming from a downstream wrapper or a
  different consumer.
- **The v1.3 map's `## Drive-by` block recorded two
  test-maintenance findings** (broken `Settings(...)`
  call and `patch_page_lines` byte-count drift) that
  pre-date v1.4 and v1.5. Carrying forward as "known
  backlog"; not in v1.5's scope.
- **Defect 2 from the user's 2026-08-31 bug-list
  (Discord DOM timing on the Mav Report)** is **not
  a bridge defect**. The Mav Report / Discord scraping
  tooling's DOM hadn't fetched the late-session posts
  when the Mon 31 Aug cut ran; the addendum captured
  the 22:37–22:46 batch. This lives at the Mav Report
  tooling layer, not the bridge. The lesson (two
  monthly snapshots + a diff) is a tooling-process
  fix, not a bridge ticket. Logged here so a future
  session doesn't try to file it on the bridge map.
- **Defect 3 from the user's 2026-08-31 bug-list
  (no standing rule ceiling for cash)** is **not a
  bridge defect**. Standing rules 12 caps the trading
  bucket at 20% and the metals sleeve at 20% but
  has no rule about cash; Mav's "increase cash to
  30%, deploy late October" is therefore fully
  compatible with current rules. The Mental Model
  Collection flagged M.5 (inverse funds, promotable
  as Rule 13) and the cash-30% trigger is a
  *separate* Rule 13 candidate. This lives at the
  journal / standing-rules layer, not the bridge.
  Future-Sunday-review question, not a bridge
  ticket.

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

### T39. Normalize `name` inputs to handle the `.md`-suffix split (b1, b6)

> **Labels**: `wayfinder:task`
> **Type**: AFK
> **Assignee**: pi (claimed + shipped same session, 2026-08-31)
> **Status**: 🟢 closed — shipped 2026-08-31 (option A + feedback-loop signal)
> **Question**: How does the bridge reconcile the `.md`-suffix split — `read_page("Foo")` returns 500, `read_page("Foo.md")` works — so an agent caller doesn't have to know which tool needs which suffix?
>
> **Context**: SB stores pages as `*.md` on disk and the
> `/.fs` HTTP API keys by the exact name the caller
> passes. So `read_page("Foo")` 500s (because
> `/.fs/Foo` doesn't exist), `read_page("Foo.md")`
> returns the body, and the same asymmetry hits
> `page_exists` and `list_tasks` (per-page form). The
> agent reporter's session-1 false-negative is this
> exact loop: `page_exists("Trading Book")` returned
> 500, the agent treated it as transient, and never
> tried `.md`.
>
> The bridge already has a normalization helper on
> the journal-gated `find_backlinks` —
> `_normalize_link_target` (T35, journal.py:1180) —
> that strips leading/trailing slashes + a trailing
> `.md` before matching. That helper is *the other
> direction* (strip `.md` from a wikilink target to
> canonicalize for lookup), but the pattern is the
> same: a pure function in front of the SB call,
> surface-level invisible to a caller that already
> passes canonical names. T39 lifts the *same shape*
> (pure function in front of every `name`-taking
> tool) but in the *add* direction (append `.md`
> to names without an extension).
>
> **Goal**: a single `_normalize_page_name(name)`
> helper in `server.py`, threaded into every
> `name`-taking tool at the top of each handler
> (before the SB round trip, before
> `_check_body_size`). The helper applies the rule:
> strip leading/trailing whitespace; if the result
> has no `.` in its basename, append `.md`.
> Pass-through for any name that already has an
> extension (`.txt`, `.json`, etc.) so non-markdown
> files don't get a spurious suffix. The helper is
> idempotent and surface-level invisible — a caller
> that already passes `Foo.md` sees no change.
>
> **Design call** (must resolve before claiming):
>
> - **A. Auto-append `.md` if missing** (recommended;
>   matches the existing `_normalize_link_target`
>   pattern).
> - **B. 404 fallback — try once with `.md` if the
>   bare miss fails.** Two round-trips on a miss,
>   zero on a hit. Most "magic."
> - **C. Clearer error string** — `page not found: Foo
>   (did you mean Foo.md?)`. No semantic change.
> - **D. Docs only** — `read_page`'s description gains
>   a sentence about the suffix. Cheapest.
>
> The skill recommends picking one in the chart pass
> and locking it; since the user didn't pick, the
> next session grills before claiming.
>
> **Done when** (assuming option A; B/C/D substitute
> the corresponding behavior):
>
> - `_normalize_page_name` helper at module scope in
>   `server.py`. Pure function; idempotent; no log
>   output; no metrics. Unit tests covering the
>   eight cases that matter: empty, whitespace-only,
>   already-canonical (`Foo.md`), needs-append
>   (`Foo`), non-md extension (`Foo.txt`),
>   multi-dot (`Foo.tar.gz`), leading/trailing
>   whitespace (`  Foo  ` → `Foo` → `Foo.md`),
>   nested path (`Areas/Foo` → `Areas/Foo.md`).
> - Threaded into `read_page`, `page_exists`,
>   `write_page`, `create_page`, `append_to_page`,
>   `prepend_to_page`, `patch_page_lines`,
>   `patch_page_replace`, `move_page` (both `name`
>   and `new_name`), `delete_page`, `check_task`
>   (both `page` and the wikilink ref's destination
>   when matched), `list_tasks` (per-page form).
>   Total: 12 call sites, all mechanical.
> - Layer-1 test: a caller passing
>   `read_page("Foo")` against a SB with `Foo.md`
>   present gets the body of `Foo.md`. A caller
>   passing `read_page("Foo.md")` against the same
>   SB gets the same body (idempotent).
> - Layer-1 test: a caller passing
>   `page_exists("Foo")` against a SB with `Foo.md`
>   present gets `True`. A caller passing
>   `page_exists("Foo")` against a SB without
>   `Foo.md` gets `False`.
> - Layer-1 test: `write_page("Foo", "hello")` against
>   an empty SB creates `Foo.md` (the agent sees the
>   `PageMeta.name` field echo `Foo.md` per the
>   helper's normalization-on-write rule).
> - Layer-1 test: `write_page("Foo.txt", "hello")`
>   against an empty SB creates `Foo.txt`, NOT
>   `Foo.txt.md`. The extension-detection rule.
> - The `MCPServer.instructions` block is *not*
>   updated in T39 (T41 handles that, independently).
>   The T39 changes are local to the tool handlers.
>
> **Files when resolved**:
> `src/mcp_silverbullet/server.py` (new
> `_normalize_page_name` helper; threaded into all
> 12 call sites), `tests/test_tools_in_memory.py`
> (Layer-1 cases), `docs/design.md` (§ Tools row
> updates for each affected tool), `CHANGELOG.md`
> (loud entry about the behavior change), `docs/
> wayfinder/map-v1.5.md` (resolution entry).
>
> **Blocks on**: the design call (A / B / C / D).
> **Unblocks**: T40 (T40's empty-name guards
> reference T39's helper for consistency); T41
> (T41's `instructions`-block sentence about
> `.md` suffix assumes T39 has shipped).
>
> **Out of scope** (deliberately): changing SB's
> URL handling (SB feature, not a bridge feature);
> adding a `name=` alias to wikilinks (different
> problem, journal-gated); changing the wire shape
> of `PageMeta.name` (the helper normalizes on
> the way in; `PageMeta.name` echoes whatever was
> passed to SB, which is the normalized form — no
> separate field).

**Resolution** (positive, 2026-08-31): shipped in
`src/mcp_silverbullet/server.py` and `tests/`. The user
picked option **A** (auto-append `.md` to bare names)
plus a **feedback-loop signal** so the bridge tells
the agent what it changed — the agent learns the
convention for its next call. Implementation matched
the ticket's charter exactly:

- New module-private
  `_normalize_page_name(name: str) -> str` helper in
  `src/mcp_silverbullet/server.py` at module scope.
  Pure, idempotent, surface-level invisible when the
  caller already passes a canonical name. Rule:
  strip leading/trailing whitespace; if the basename
  (the segment after the last `/`) has no `.`, append
  `.md`. So `Foo` → `Foo.md`, `Areas/Foo` →
  `Areas/Foo.md`, `Foo.txt` stays `Foo.txt`,
  `Foo.tar.gz` stays `Foo.tar.gz`, `.gitignore`
  stays `.gitignore`.
- New module-private
  `_name_resolution_payload(requested, resolved)`
  helper that returns an empty dict when the caller's
  input was already canonical (no `name_resolution`
  field added) or
  `{"name_resolution": {"requested": …, "resolved": …,
  "suffix_added": …}}` when the bridge changed the
  name. `suffix_added` is `".md"` when the bridge
  appended the canonical extension, `None` when the
  helper only stripped whitespace. The field is
  *conditional* — existing wire-shape assertions on
  the success envelope continue to pass byte-for-byte
  for canonical callers.
- Threaded into 12 call sites across 10 tools plus
  the resource template and the list-pages hydration
  walker: `read_page`, `page_exists`, `write_page`,
  `create_page`, `delete_page`, `append_to_page`
  (live + dry-run), `prepend_to_page` (live +
  dry-run), `patch_page_lines` (live + dry-run),
  `patch_page_replace` (live + dry-run),
  `move_page` (both `name` and `new_name`, plus the
  same-name short-circuit compares the *resolved*
  names), `diff_pages` (both `name` and `other_name`),
  `check_task` (`page` only — the `ref` argument is
  a wikilink target, handled by the existing
  `_normalize_link_target` canonicalization), the
  per-page form of `list_tasks`, and the
  `silverbullet://page/{name}` resource template.
  The `_hydrate_list_etags` walker also normalizes
  the row's `name` before the per-page GET so list
  rows with bare names hydrate successfully against
  the canonical file.

One design decision the user pinned that the chart
session left open: **error wording references the
*resolved* name, not the caller's input**. A caller
passing `name="Foo"` that 404s sees
`ToolError("page not found: Foo.md")` (the canonical
form the bridge tried), not
`ToolError("page not found: Foo")` (the caller's
input). The agent sees the same canonical form in
errors that it sees in successes — consistent
feedback. The `name_resolution` envelope makes the
normalization explicit on the success path; errors
reference the canonical form because that's what the
bridge actually hit.

The full test surface (14 new T39 Layer-1 cases in
`tests/test_tools_in_memory.py`) covers: bare-name
resolution, canonical-name idempotence, non-md
extension passthrough, whitespace stripping, nested
path resolution, `page_exists` resolution, write
canonicalization for bare / extension / nested
inputs, `move_page` source + destination resolution,
same-name short-circuit on resolved names,
`list_tasks` per-page form, `check_task` `page`
resolution, resource template resolution, and
`diff_pages` both-side resolution. All 479
non-live-e2e tests pass; no live-e2e tests run
(this dev-box SB is unavailable in the chart
session's nix dev env).

README's roadmap block doesn't need updating (the
T39 entry is already at the top of v1.5's bullet
list); CHANGELOG gained the v1.5 `[Unreleased]`
section with full migration notes (T39 is
behavior-changing for any v1 / v1.1 / v1.2 / v1.3
caller that explicitly passes `name="Foo"` expecting
a 500-shaped error — the bridge now resolves and
returns 200; the changelog calls this out loudly).
`docs/design.md` § Tools gained a short paragraph
on T39 normalization pointing operators at the
handler docstrings for per-tool threading detail.
`docs/wayfinder/map-v1.5.md` T39 status closed
(green); `## Not yet specified` cleared the resolved
design-call / extension-rule / error-wording fog
patches (T42 threshold values and the
`instructions`-block update remain as the live fog).

T40 unblocked (its empty-input guards now reference
T39's helper for ordering clarity); T41 unblocked
(its `instructions`-block sentence assumes T39 has
shipped); T42 unchanged (independent).

---

### T40. Lift upfront empty-input validation across the remaining write tools (b9)

> **Labels**: `wayfinder:task`
> **Type**: AFK
> **Assignee**: *(unclaimed)*
> **Status**: 🟡 open — unblocked, on the frontier
> **Question**: How does every write tool gain the
> same upfront empty-input guard that
> `create_page` / `append_to_page` /
> `prepend_to_page` / `patch_page_replace` /
> `check_task` already ship?
>
> **Context**: The bug reporter's b9 surfaces a
> real gap on the current code:
> `write_page(name="", content="test")` and
> `write_page(name="x", content="")` both reach SB
> and 500. The fix is mechanical — lift the
> existing pattern from the tools that already
> have it:
>
> - `create_page`: empty `name` →
>   `ToolError("name must not be empty")` (line
>   1192).
> - `append_to_page`: empty `text` →
>   `ToolError("text must not be empty")` (line
>   1324).
> - `prepend_to_page`: empty `content` →
>   `ToolError("content must not be empty")` (line
>   1437).
> - `patch_page_replace`: empty `find` →
>   `ToolError("find must not be empty")` (line
>   1686).
> - `check_task`: empty `ref` →
>   `ToolError("ref must not be empty")` (line
>   2235).
>
> The four tools that don't have it yet:
>
> - `write_page`: needs `name` + `content` guards.
> - `move_page`: needs `name` + `new_name` guards.
> - `delete_page`: needs `name` guard.
> - `patch_page_lines`: needs `name` guard.
> - `patch_page_replace`: also needs `new_string`
>   guard (already has `find`).
>
> **Goal**: two helpers at module scope —
> `_validate_nonempty_name(name)` (the
> name-shape guard) and `_validate_nonempty_value(
> value, label)` (the body-shape guard, parameterized
> on the parameter name so the error message can
> say `text must not be empty` or `content must not
> be empty` depending on which tool called it) —
> threaded into each handler at the top, *before*
> T39's normalization helper (so a caller passing
> `name=""` still sees the loud empty-name error
> rather than the normalized form `".md"`).
>
> **Done when**:
>
> - `_validate_nonempty_name` and
>   `_validate_nonempty_value` helpers at module
>   scope in `server.py`. Mirror the wording of
>   the existing inline guards (no new error
>   shape; just consolidation).
> - Threaded into the 5 tools listed above.
>   Total: 7 call sites (one per `name` argument,
>   one per `content` / `text` / `new_string`
>   argument that needs guarding).
> - Layer-1 test per affected tool: empty
>   `name` → `ToolError("name must not be empty")`
>   upfront, no SB round trip (PUT counter stays
>   at zero). Empty `content` / `text` /
>   `new_string` → `ToolError("... must not be
>   empty")` upfront, no SB round trip.
> - Layer-1 test: whitespace-only `name` →
>   same error (the guard rejects
>   `name.strip() == ""`).
> - Existing tests for the already-guarded tools
>   (`create_page` / `append_to_page` / etc.)
>   continue to pass — the new helpers are
>   additive, not replacing inline guards.
>
> **Files when resolved**:
> `src/mcp_silverbullet/server.py` (new helpers
> + 7 threaded call sites), `tests/test_tools_
> in_memory.py` (Layer-1 cases), `docs/design.md`
> (§ Tools row updates), `docs/wayfinder/
> map-v1.5.md` (resolution entry).
>
> **Blocks on**: nothing — but reads cleaner
> after T39 ships so the helpers' ordering
> against T39's normalizer is settled (empty
> guards fire on the caller's raw input;
> normalization fires on the validated input).
> Can claim either order. **Unblocks**: nothing —
> terminal ticket.

### T41. Doc clarifications: `index.md` source-vs-render, `move_page` no-op, `.md`-suffix convention (b5, b8)

> **Labels**: `wayfinder:task`
> **Type**: AFK
> **Assignee**: *(unclaimed)*
> **Status**: 🟡 open — unblocked, on the frontier
> **Question**: How do three small documentation
> gaps — `read_page` returning template source
> instead of rendered output (b5), `move_page`'s
> `name == new_name` no-op ignoring `if_match` (b8),
> and the `.md`-suffix convention (lifted into
> v1.5 by T39) — get clarified at the points of
> contact?
>
> **Context**: Three small, sharp doc gaps. Each
> is one sentence in the affected tool
> description (or the bridge's
> `MCPServer.instructions` block for the suffix
> convention). No code changes; just strings.
>
> - **b5**: `read_page("index.md")` returns a body
>   containing `${template.each(...)}` literally.
>   This is correct behavior — the bridge is a
>   transport, not a renderer; it returns whatever
>   SB stored. But an agent reading the page for
>   the first time sees "broken" template syntax.
>   One sentence in `read_page`'s description
>   closes the gap.
> - **b8**: `move_page("Foo", "Foo")` is a no-op
>   that ignores `if_match`. The behavior is
>   documented in `server.py::move_page`'s
>   description, but the "ignores `if_match`" half
>   is non-obvious — a caller passing
>   `if_match=<expected_etag>` on a no-op might
>   expect the no-op to surface as a 412 if the
>   page has drifted. It doesn't (no write
>   happens, no precondition check fires). One
>   sentence closes the gap.
> - **Suffix convention**: now that T39 ships
>   (assuming option A), the bridge's
>   `MCPServer.instructions` block needs a single
>   sentence noting that the bridge resolves
>   bare-name inputs to `*.md` for the agent's
>   convenience. Without this, an agent that
>   tries `read_page("Foo")` after T39 will see a
>   successful response and not understand why
>   (its input had no extension; the bridge added
>   one).
>
> **Goal**: targeted additions to three tool
> descriptions + one `instructions`-block
> sentence. No new code beyond the description
> strings.
>
> **Done when**:
>
> - `read_page`'s description gains: "Pages
>   containing `${template.each(...)}` (or other
>   Space Lua template syntax) are returned as raw
>   markdown source, never as rendered output —
>   the bridge is a transport, not a renderer."
> - `move_page`'s description gains: "`name ==
>   new_name` is a no-op that ignores `if_match`
>   — no write happens, so no precondition check
>   fires. The T23 ack envelope is returned
>   verbatim from a re-read of the source page."
>   (Or whatever the existing description's
>   phrasing suggests; T41 doesn't invent new
>   wording, just clarifies the existing one.)
> - `MCPServer.instructions` gains: "Page names
>   passed without a file extension are
>   automatically suffixed with `.md` (T39); names
>   with an existing extension (`Foo.txt`) pass
>   through unchanged." (This sentence assumes
>   T39 has shipped with option A. If T39 lands
>   with B/C/D, T41's wording shifts to match.)
>
> **Files when resolved**:
> `src/mcp_silverbullet/server.py` (description
> string updates), `docs/wayfinder/map-v1.5.md`
> (resolution entry).
>
> **Blocks on**: T39 (the suffix-convention
> sentence in `instructions` assumes T39 has
> shipped; the other two sentences don't block).
> **Unblocks**: nothing — terminal ticket.
>
> **Out of scope** (deliberately): rewriting
> the existing 50-line `instructions` block
> (T41 is a targeted addition; the block is
> locked at v1 and a wholesale rewrite is a
> bigger lift than this map warrants); updating
> the README's tool inventory descriptions
> separately from the source-level descriptions
> (the README's table already cross-links to the
> source; T41 doesn't add a second copy).

### T42. 412 contention hint: surface "you are in a contention window" to the agent (b10)

> **Labels**: `wayfinder:task`
> **Type**: AFK
> **Assignee**: *(unclaimed)*
> **Status**: 🟡 open — unblocked, on the frontier
> **Question**: How does the bridge signal to an
> agent that it's hitting the same page's 412
> precondition over and over — without changing
> the standard 412 error shape that already
> works?
>
> **Context**: The bug reporter's W36 page races
> with another editor (probably the SB UI or a
> second agent instance). Every few minutes, an
> `if_match` write returns 412. Today the agent
> has to pattern-match
> `precondition failed; check if_match/
> if_none_match` and either retry blindly or back
> off heuristically. There's no signal that "you
> are in a contention window."
>
> T42 adds a thin rate-limiter keyed on
> `(name,)` with a 60-second sliding window
> counting 412s on writes that threaded an
> explicit `if_match=<etag>`. After N=3
> consecutive 412s on the same page within
> M=60 seconds, the bridge adds a
> `concurrent_edit_hint: true` field to the
> standard 412 `ToolError`'s envelope, so an
> agent in a contention window gets a clear
> signal to back off.
>
> The hint is **advisory, never authoritative.**
> The bridge still raises the standard 412
> `ToolError` regardless of whether the hint
> fires. The hint is a
> `concurrent_edit_hint: bool` field on the
> error envelope that an agent *can* check to
> back off — a future caller that doesn't check
> the field sees no change. The hint doesn't
> change the wire shape of the success path; it
> only adds an optional field to the error
> envelope that MCP clients can ignore.
>
> **Goal**: a thin `collections.deque`-backed
> counter at module scope in `server.py`,
> threaded into `_translate_sb_errors`'s 412
> branch. The counter is per-process (not
> persistent across restarts; not shared across
> replicas — both fine for a single-user
> bridge).
>
> **Done when**:
>
> - `_CONTENTION_WINDOW_SECONDS = 60` and
>   `_CONTENTION_THRESHOLD = 3` constants at
>   module scope in `server.py`. Future tuning
>   is a one-line change, not a ticket.
> - `_contention_hint(name: str) -> bool`
>   helper: pushes the current timestamp onto
>   `name`'s deque, evicts timestamps older
>   than `_CONTENTION_WINDOW_SECONDS`, returns
>   `True` if the deque length has crossed
>   `_CONTENTION_THRESHOLD`. Side effect: a
>   bounded per-name memory footprint (one
>   deque per distinct `name`, max length =
>   `_CONTENTION_THRESHOLD`).
> - Threaded into `_translate_sb_errors`'s
>   `PreconditionFailed` clause (line 194):
>   the `ToolError` raises with the standard
>   wording, and *additionally* sets a
>   `concurrent_edit_hint: bool` attribute on
>   the `ToolError` object if the helper
>   returns `True`.
> - The MCP SDK's `ToolError` surface gains
>   the `concurrent_edit_hint` field in the
>   error envelope when set. (Verify by
>   reading `mcp.server.mcpserver.exceptions
>   .ToolError` — if the SDK doesn't natively
>   surface arbitrary fields, T42 may need a
>   thin wrapper class.)
> - Layer-1 test: 4 consecutive 412s on the
>   same page in <60s; the 4th carries
>   `concurrent_edit_hint: True`. The first 3
>   carry `False`.
> - Layer-1 test: 1 412 on page A, 1 412 on
>   page B; neither carries the hint (the
>   counter is per-page, not global).
> - Layer-1 test: 3 412s on the same page,
>   then a 60s wait, then a 4th 412; the 4th
>   carries `False` (the sliding window has
>   evicted the old timestamps).
> - The hint is **never** raised on the
>   success path; a successful write after
>   three 412s still returns the T23 ack
>   envelope with no hint field.
>
> **Files when resolved**:
> `src/mcp_silverbullet/server.py` (new
> constants + helper + thread into 412 clause),
> `tests/test_tools_in_memory.py` (Layer-1
> cases), `docs/design.md` (§ Tools § Status-
> code mapping note on the 412 row),
> `docs/wayfinder/map-v1.5.md` (resolution
> entry).
>
> **Blocks on**: nothing — T42 is independent
> of T39 / T40 / T41. **Unblocks**: nothing —
> terminal ticket.
>
> **Out of scope** (deliberately): persisting
> the contention counter across restarts
> (process-local is fine for a single-user
> bridge; persistence is a bigger lift);
> sharing the counter across replicas (no
> multi-replica deployment in the v1.x
> posture); changing the wire shape of the
> success path (the hint only ever appears
> on 412 errors, never on 200 responses);
> replacing the standard 412 wording with a
> hint-flavored variant (the hint is additive,
> not replacing).

---

### T43. CF 5xx wrapper JSON: parse `retry_after` and surface as `cf_hint` on `ServerError` (b11)

> **Labels**: `wayfinder:task`
> **Type**: AFK
> **Assignee**: *(unclaimed)*
> **Status**: 🟡 open — unblocked, on the frontier
> **Question**: How does the bridge defend against
> the messy CF 5xx wrapper JSON leaking into the
> agent's error stream when CF (in front of SB)
> 502s / 503s / 504s — by parsing the body, extracting
> the useful bits (`retry_after`, `error_code`,
> `title`), and surfacing them as a `cf_hint`
> field on the `ServerError` envelope?
>
> **Context**: The user reported (session log
> 2026-08-31) an actual 502 from Cloudflare with
> the full wrapper JSON surfacing in the MCP
> wrapper's error stream:
>
> ```
> MCP: Failed to refresh kesor: Error POSTing
> to endpoint: {"type":"https://...",
> "title":"Error 502: Bad gateway","status":502,
> "detail":"...","instance":"a33e...",
> "error_code":502,"error_name":"origin_bad_gateway",
> "error_category":"origin","ray_id":"a33e...",
> "timestamp":"2026-08-31T19:40:15Z",
> "zone":"sb.kesor.net","cloudflare_error":true,
> "retryable":true,"retry_after":60,
> "owner_action_required":true,"what_you_should_do":
> "**Wait and retry.**...","footer":"..."}
> ```
>
> The wrapper saw the raw CF JSON because the
> wrapper was making its own POST against
> `sb.kesor.net` (the CF-fronted SB) and CF
> returned 502 directly — the bridge wasn't in the
> path. So this is partly a wrapper-side concern
> (the wrapper should truncate the body), but
> it's also a bridge-side concern: when the
> *bridge* itself makes an outbound SB call (via
> the operator's `SB_URL` pointing at a CF-fronted
> instance), the bridge sees the same CF 5xx and
> the same JSON body, and currently throws the
> body away entirely (raising
> `ServerError("silverbullet error: 502")` with
> no body attached).
>
> The map's existing `## Out of scope` entry on
> **b3** already flagged the upstream-body-leak
> pattern as a wrapper-side issue:
> "the reporter's MCP wrapper is unwrapping
> `ToolError` and showing the underlying
> `ServerError`'s body." b11 is the bridge-side
> defense against the same pattern: if the
> bridge detects a CF-shaped 5xx body, it parses
> the JSON and surfaces the useful bits as a
> `cf_hint` field on the error envelope. An
> MCP-SDK-aware wrapper (which Grok's connector
> is) sees the structured envelope and can
> surface the hint cleanly; a wrapper that
> unwraps to raw body still gets the same
> CF JSON, but the bridge has done its part
> by giving the structured envelope.
>
> Same shape as T42's `concurrent_edit_hint`:
> **advisory, never authoritative.** The bridge
> still raises the standard 412 / 5xx
> `ToolError` regardless of whether the hint
> fires. The hint is a `cf_hint: dict` field on
> the error envelope that an agent *can* check
> to back off — a future caller that doesn't
> check the field sees no change. The hint
> doesn't change the wire shape of the success
> path; it only adds an optional field to the
> 5xx error envelope.
>
> **Goal**: extend `_translate_sb_errors`'s
> `ServerError` clause (and any future 5xx
> translation points) to attach a `cf_hint`
> field when the underlying response body
> looks CF-shaped. The hint carries the parsed
> `retry_after` (seconds), `error_code`
> (string), and `title` (string) — the three
> fields that are useful to an agent deciding
> whether to retry. Other CF fields (`ray_id`,
> `zone`, `instance`, etc.) are intentionally
> dropped — they're useful for debugging the
> CF/proxy layer, not for the agent's
> decision-making.
>
> **Done when**:
>
> - New `_parse_cf_error(body: str) -> dict |
>   None` helper in `sb_client.py`: returns
>   `None` if the body doesn't look CF-shaped
>   (no `cloudflare_error` / `error_category` /
>   `ray_id` field, or body isn't valid JSON),
>   else returns a dict like
>   `{"retry_after": 60, "error_code": 502,
>   "title": "Error 502: Bad gateway"}`. Only
>   the three useful fields are surfaced;
>   everything else is dropped.
> - `ServerError` gains an optional
>   `cf_hint: dict | None = None` field. The
>   `_raise_for_status` helper checks
>   `response.text` after a 5xx; if it's
>   CF-shaped, it populates `ServerError.cf_hint`
>   before raising.
> - `_translate_sb_errors`'s `ServerError`
>   clause in `server.py` reads `exc.cf_hint`
>   and, when present, attaches a `cf_hint`
>   field to the `ToolError` envelope (same
>   pattern as T42's `concurrent_edit_hint`).
>   When `cf_hint` is `None`, the envelope is
>   unchanged (no new field — backwards compat
>   for non-CF-fronted setups).
> - Layer-1 test: SB returns 502 with a
>   CF-shaped body; the bridge raises
>   `ToolError("silverbullet error: 502")`
>   with `cf_hint={"retry_after": 60,
>   "error_code": 502, "title": "Error 502:
>   Bad gateway"}` on the envelope.
> - Layer-1 test: SB returns 500 with a
>   plain-text body (not CF-shaped); the
>   bridge raises `ToolError("silverbullet
>   error: 500")` with no `cf_hint` field.
> - Layer-1 test: SB returns 502 with a
>   non-CF JSON body (random other JSON);
>   the bridge raises `ToolError("silverbullet
>   error: 502")` with no `cf_hint` field
>   (the helper detects the missing
>   CF-marker fields and returns `None`).
> - Layer-1 test: SB returns 502 with an
>   empty body; the bridge raises
>   `ToolError("silverbullet error: 502")`
>   with no `cf_hint` field (no body to
>   parse).
> - Layer-1 test: SB returns 502 with a
>   CF-shaped body that omits `retry_after`;
>   the bridge raises
>   `ToolError("silverbullet error: 502")`
>   with `cf_hint={"retry_after": None,
>   "error_code": 502, "title": "..."}` —
>   the field is always present when the
>   body is CF-shaped, but its value can
>   be `None` if the upstream didn't
>   include it.
> - The hint is **never** raised on the
>   success path; a 200 response after a
>   502 returns the T23 ack envelope with
>   no `cf_hint` field.
>
> **Files when resolved**:
> `src/mcp_silverbullet/sb_client.py` (new
> `_parse_cf_error` helper; `ServerError.cf_hint`
> field; `_raise_for_status` populates it),
> `src/mcp_silverbullet/server.py`
> (`_translate_sb_errors`'s `ServerError` clause
> attaches `cf_hint` to the `ToolError` envelope),
> `tests/test_sb_client.py` (Layer-1 cases for
> `_parse_cf_error`), `tests/test_tools_in_memory
> .py` (Layer-1 cases for the envelope),
> `docs/design.md` (§ Tools § Status-code mapping
> row on 5xx gains a `cf_hint` field note),
> `docs/wayfinder/map-v1.5.md` (resolution
> entry).
>
> **Blocks on**: nothing — T43 is independent
> of T39 / T40 / T41 / T42. **Unblocks**:
> nothing — terminal ticket.
>
> **Out of scope** (deliberately): fixing
> the wrapper-side leak (the wrapper sees
> the raw CF body directly when its own
> POST to CF 502s — that's not a bridge
> code path; the wrapper fix lives at the
> wrapper layer, documented as a known
> issue in `docs/cloudflare-setup.md`);
> adding automatic retry-on-5xx (the agent
> decides when to retry based on
> `cf_hint.retry_after`, matching T42's
> pattern of "hint, never action"); changing
> the wire shape of the success path (the
> hint only appears on 5xx errors, never on
> 200 responses); surfacing non-CF error
> bodies verbatim (the bridge currently
> throws the body away on 5xx, which is the
> correct conservative posture — surfacing
> upstream bodies adds noise without a
> useful signal); persisting the parsed
> CF hint across retries (the bridge
> re-parses on each 5xx; no cross-call
> state needed).

---

### T44. Fix T31b's false-positive "concurrent edit detected" (b12)

> **Labels**: `wayfinder:task`
> **Type**: AFK
> **Assignee**: *(unclaimed)*
> **Status**: 🟡 open — unblocked, on the frontier
> **Question**: How does the bridge stop
> raising `concurrent edit detected` on every
> successful write — by changing T31a's
> synthesized etag to drop the mtime component
> (use only `X-Content-Length`) so re-reads of
> the same body return the same etag, while
> preserving the "different body → different
> etag" property the concurrency primitive
> relies on?
>
> **Context**: User report 2026-08-31 (Mav
> Report defect list, Mental Model defects
> list): `patch_page_replace` (and by
> extension every read-modify-write tool
> that auto-threads the read's etag into
> `if_match` — `append_to_page`,
> `prepend_to_page`, `patch_page_lines`,
> `check_task`) returns
> `ToolError("concurrent edit detected: ...")`
> on at least four of the user's writes where
> the underlying write actually succeeded. The
> user's workaround: re-read the page and diff
> the body. The Grok Automations script handles
> this transparently (it collapses the
> duplicate), but the bridge should not lie
> about 412 in the first place.
>
> Reproduction (confirmed in this session's
> chart work): a Layer-1 mock that returns a
> stable `X-Last-Modified` on the pre-write
> GET and a fresh `X-Last-Modified` (the
> bridge's `X-Last-Modified` request header,
> which the bridge stamps with `now_ms`) on
> the PUT response and the post-write
> verification GET, sees T31b fire on every
> write. The pre-write read's synthesized
> etag is `"{pre_mtime}-{size}"`; the
> post-write re-read's is `"{now_ms}-{size}"`;
> `T31b` compares them and raises. Confirmed
> in `tests/test_tools_in_memory.py` —
> `test_t31b_write_page_verification_passes_when_etag_unchanged`
> passes only because its mock returns the
> *same* etag on both the PUT and the
> verification GET, hiding the bug from the
> test surface.
>
> **Root cause**: T31a's synthesized etag
> uses `{X-Last-Modified}-{X-Content-Length}`
> (see `sb_client.py::synthesize_etag`, the
> `synthesize_etag` helper docstring). The
> bridge stamps `X-Last-Modified` with
> `now_ms` on every PUT
> (`sb_client.py:411`, `_WRITE_HEADERS`),
> independent of whether the body changed.
> Re-reads after a write therefore return a
> different `X-Last-Modified` than the
> pre-write read, even when no concurrent
> edit happened — so T31b's verification
> (`_verify_concurrency_token` in
> `server.py:392-509`) raises "concurrent
> edit detected" on every successful write.
>
> The T31a docstring justified including
> `X-Last-Modified` as the "anchor" for the
> synthesized etag: "a body-length-derived
> etag would be unstable across reads of the
> same body (an SB that doesn't emit
> `X-Last-Modified` can't tell the agent
> *when* a write happened, so the agent
> can't tell a stale read from a fresh one)."
> The reasoning was overcautious: for the
> concurrency primitive to work, the etag
> only needs to differ between two *different*
> bodies — and `X-Content-Length` alone
> satisfies that (same body → same size →
> same etag; different body → different size
> → different etag). The `X-Last-Modified`
> component was tracking "when" rather than
> "what", and the "when" component is what
> drifts on every write.
>
> **Goal**: change T31a's synthesized etag
> to use only `X-Content-Length`. Format:
> `"{size_bytes}"` (a single quoted integer
> string, e.g. `'"42"'`). Drop the
> `{last_modified_ms}-{size_bytes}` shape.
> Re-reads of the same body return the same
> size → same synthesized etag → T31b's
> comparison passes. Re-reads after a body
> change return a different size → different
> synthesized etag → T31b fires "concurrent
> edit detected" (the correct signal).
>
> **Wire-shape change**: the synthesized
> etag format changes from
> `"{mtime}-{size}"` to `"{size}"`. This is
> a backwards-incompatible change for any
> v1.3 caller holding a synthesized etag
> across calls — the old format's `mtime`
> is meaningless once the etag drops it,
> and any caller that stored the old etag
> and threaded it back will see a mismatch.
> Migration: callers that persist an etag
> across calls should re-read once after the
> T44 fix lands (the new etag is the
> canonical one going forward). CHANGELOG
> entry loud about this.
>
> **Done when**:
>
> - `synthesize_etag(last_modified_ms,
>   size_bytes)` returns `"{size_bytes}"`
>   when `size_bytes` is not None, else
>   `None`. The `last_modified_ms` parameter
>   is kept (so the call sites don't need
>   to change) but is unused. The docstring
>   is rewritten: drops the "anchor"
>   reasoning, explains why size alone is
>   the correct concurrency primitive, notes
>   the wire-shape change for v1.3 callers.
> - `_etag_from_response(response)` calls
>   `synthesize_etag` with the same args as
>   today; the helper's contract is
>   unchanged (it still returns a string or
>   `None`), only the *content* of the
>   string changes.
> - `write_page` returns `etag=f"{size}"`
>   instead of `etag=f"{mtime}-{size}"`;
>   existing tests in `test_sb_client.py`
>   that assert the dashed form are updated
>   to assert the bare-size form. The
>   `test_if_match_synthetic_etag_drifts_on_body_change`
>   live test (in `test_e2e_live_sb.py`)
>   continues to assert drift on body change
>   — same primitive, different format.
> - T31b's verification (`_verify_concurrency_token`)
>   now works correctly: the post-write
>   re-read returns an etag with the same
>   size as the pre-write read (when no
>   concurrent edit happened), the
>   comparison passes, the tool returns the
>   T23 ack envelope. **No code change to
>   T31b itself** — the fix lives entirely
>   in `synthesize_etag`.
> - Layer-1 test reproducing the user's
> defect: a mock that returns a stable
>   `X-Last-Modified` on the pre-write GET
>   and a *different* `X-Last-Modified` (the
>   bridge's request header) on the PUT
>   response and the post-write GET, with
>   the body unchanged, asserts the patch
>   returns the T23 ack envelope with
>   `is_error=False`. This is the
>   `test_t31b_write_page_verification_passes_when_etag_unchanged`
>   test updated to use a *realistic* mock
>   (different mtimes on the two reads), not
>   a contrived same-etag mock. Same for
>   the `test_t31b_write_page_detects_concurrent_edit_via_silent_overwrite`
>   test: the drift detection now relies on
>   size change (different body → different
>   size → different synthesized etag → T31b
>   fires), which is the correct semantic
>   anyway.
> - Layer-1 test for the size-only
>   synthesized etag: same body on two
>   reads returns the same synthesized etag;
>   different bodies return different
>   synthesized etags. Lock the new
>   primitive.
> - The existing
>   `test_t31b_*` tests in
>   `test_tools_in_memory.py` are updated
>   to match the new format; the existing
>   `test_t31b_*` tests in
>   `test_sb_client.py` for the
>   `synthesize_etag` helper are updated to
>   assert `"{size}"` instead of
>   `"{mtime}-{size}"`.
> - CHANGELOG entry under
>   `[Unreleased]` (v1.5): **"Breaking:
>   synthesized etag format change"** —
>   loud migration note that any caller
>   holding a v1.3 synthesized etag across
>   calls should re-read after the fix
>   lands.
>
> **Files when resolved**:
> `src/mcp_silverbullet/sb_client.py`
> (`synthesize_etag` rewritten; tests in
> `tests/test_sb_client.py` updated),
> `tests/test_tools_in_memory.py`
> (T31b test mocks updated to use
> realistic mtime drift; new Layer-1
> test for the size-only primitive),
> `docs/design.md` (§ SilverBullet client
> contract: synthesized etag format
> section rewritten),
> `CHANGELOG.md` (loud migration entry),
> `docs/wayfinder/map-v1.5.md` (resolution
> entry).
>
> **Blocks on**: nothing — T44 is
> independent of T39 / T40 / T41 / T42 / T43.
> **Unblocks**: nothing — terminal ticket.
>
> **Out of scope** (deliberately):
> detecting pre-write concurrent edits on
> SBs that strip `ETag` (the synthesized
> etag's mtime drift makes that detection
> unreliable regardless of T31a's format;
> the honest primitive is "did the body
> change?" not "did the mtime change?");
> removing T31b entirely (the helper still
> catches the *post-write* race — someone
> else writes between the bridge's write
> and the verification GET — and that's a
> useful signal even if the *pre-write*
> race is lost); keeping `last_modified_ms`
> as part of the etag for "when" semantics
> (it's the source of the bug; the
> primitive is "what", not "when");
> changing the wire shape of the *real*
> ETag path (SBs that emit `ETag` headers
> already work correctly and T44 doesn't
> touch them); auto-detecting whether to
> use the synthesized or real primitive
> (the bridge already does this in
> `_etag_from_response` — real etag wins
> when present, synthesized fallback when
> not).
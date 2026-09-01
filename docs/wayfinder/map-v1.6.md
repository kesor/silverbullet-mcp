<!--
Local-markdown tracker (v1.5's tracker lives in `map-v1.5.md`; this
map is the next effort). The v1.5 destination ("agent-experience
hardening") reached it on 2026-08-31 with six tickets all closed
— T39 (name normalization), T40 (empty-input validation), T41
(doc clarifications), T42 (412 contention hint), T43 (CF 5xx
cf_hint), T44 (synthesized-etag primitive fix). v1.4 reached
destination on 2026-09-01 with T37 (substring filter) and T38
(journal-gate discovery pointer). v1.6 narrows the v1.5 charter's
residual gap: when the agents in the W36 page pattern hit a
412 race, they currently see `precondition failed; check
if_match/if_none_match` (or `concurrent edit detected: …` on
the T31b silent-overwrite path) and give up because the error
doesn't tell them *what to do next*. One ticket: T45 enriches
both 412 paths with retry guidance and (where the bridge has
it in hand) the current etag, so an agent that followed the
concurrency protocol correctly doesn't have to do an extra
read round trip just to learn the next etag.

Standing preferences from the prior maps continue to apply
unless overridden here:

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
- The MCP SDK renders `ToolError` to the wire as plain
  `TextContent(text=str(exc))` — no native envelope field exists;
  the prior maps' T42 / T43 patterns (machine-parseable suffix
  on the existing message-text channel) continue to bind.

When in doubt, `docs/wayfinder/map.md` / `map-v1.1.md` /
`map-v1.2.md` / `map-v1.3.md` / `map-v1.4.md` / `map-v1.5.md`
are the source of truth on standing preferences; this map
inherits them.
-->

# Wayfinder Map — `mcp-silverbullet` v1.6 (412 retry guidance on the bridge surface)

## Destination

> **v1.6: 412 retry guidance on the bridge surface.** One
> ticket. Both 412 paths — the standard SB-honors-`If-Match`
> 412 (raised by `_translate_sb_errors`) and the silent-
> overwrite 412 (raised by T31b's `_verify_concurrency_token`)
> — get richer error messages that tell the agent what to do
> next. The standard-412 message gains a "call `read_page(name)`
> to get the current etag and re-issue" pointer with the page
> name inlined; the silent-overwrite 412 (which already has the
> new etag from the verification re-read but doesn't surface it)
> gains a `{current_etag}` placeholder so the agent has the
> exact `if_match=` value for the next call without an extra
> read round trip. The T42 `[concurrent_edit_hint: true]`
> suffix stays unchanged — the new wording sits *before* the
> hint marker, so an agent that pattern-matches on either the
> bare "precondition failed" wording or the marker substring
> still matches. The bridge is byte-for-byte the same on the
> happy path; only error wording changes.

The shape:

- **Standard 412 enrichment** (`_translate_sb_errors`'s
  `PreconditionFailed` clause): the current message
  (`"precondition failed; check if_match/if_none_match"`)
  gains a `; read_page({name}) for the current etag and
  re-issue` suffix. The `{name}` is the page the
  `_translate_sb_errors(name)` call closed over, so the
  agent sees a literal `read_page("Foo.md")` pointer rather
  than a generic "re-read and retry" sentence. The
  `[concurrent_edit_hint: true]` marker (T42) sits after
  the new suffix when the contention threshold trips, so the
  full 412 message becomes:
  `"precondition failed; check if_match/if_none_match;
  read_page(\"Foo.md\") for the current etag and re-issue
  [concurrent_edit_hint: true]"`.

- **Silent-overwrite 412 enrichment** (T31b's
  `_verify_concurrency_token`): the current message
  (`_CONCURRENT_EDIT_MSG`, formatted with `expected_etag`)
  gains two new placeholders — `{name}` (the page the
  write targeted) and `{current_etag}` (the post-write
  etag the helper just synthesized from the verification
  re-read). New wording:
  `"concurrent edit detected on {name}: page changed since
  you read it at {expected_etag}; current etag is
  {current_etag} — re-issue the write with
  if_match={current_etag}"`. The agent has the literal
  `if_match=` value for the next call and can retry
  without an extra read round trip — the bridge just did
  the read for them. This is the change that closes the
  user's "agents complain about it, and don't want to
  write anymore" complaint: a correctly-behaving agent that
  followed the protocol sees the bridge tell it exactly
  what the next call should look like.

- **Tests**: the existing byte-for-byte 412 wording tests
  (`test_write_page_412_returns_tool_error_with_design_doc_wording`
  and friends) update from `_text(result) == "..."` to
  `assert "precondition failed; check if_match/if_none_match"
  in _text(result)` plus the new suffix-in-message
  assertion. New Layer-1 cases:
  `test_t45_standard_412_message_includes_read_page_pointer`
  (the standard 412 path includes the page name and
  `read_page(` literal), `test_t45_concurrent_edit_message_includes_current_etag`
  (the silent-overwrite path embeds the verification-re-read
  etag), `test_t45_concurrent_edit_message_includes_if_match_retry_form`
  (the literal `if_match=` value appears so an agent can
  copy-paste), `test_t45_concurrent_edit_hint_marker_still_appends`
  (T42's marker survives the new wording — agent that
  pattern-matches on `[concurrent_edit_hint:` still matches).

- **Documentation**: `docs/design.md` § Tools § Status-code
  mapping 412 row gains a sentence noting the new wording
  shape (the `read_page({name})` pointer on the standard
  path; the embedded `{current_etag}` on the silent-
  overwrite path). `README.md` concurrency section gains
  a one-paragraph note: "On a 412, the bridge tells you
  the next call's exact `if_match=` value (silent-overwrite
  path) or the `read_page({name})` call that gives you
  that value (standard path). You don't need to guess."
  `CHANGELOG.md` v1.6 `### Changed` section records the
  wording change with the same migration posture as T42 /
  T43 / T44: existing agents that pattern-match on the
  bare `precondition failed` substring still match; only
  agents that pinned the full byte-for-byte 412 message
  (a single test in `tests/test_tools_in_memory.py`)
  need to update.

### Status

Charted 2026-09-01 in response to the user's report
("There are still lots of 412 races. The agents complain
about it, and don't want to write anymore. Can we at
least return an error telling them they should try again
and give them the correct etag or something?"). The
narrow scope (enrich both 412 messages; don't auto-retry)
was chosen at chart time per the user's answer. v1.5
reached destination on 2026-08-31 (six tickets closed:
T39 / T40 / T41 / T42 / T43 / T44); v1.4 reached
destination on 2026-09-01 (T37 + T38 closed). The
v1.6 surface is the error-wording-only refinement; no
new tools, no new env vars, no behavior change for
non-race writes.

The map has **one open ticket** (T45), unblocked, on
the frontier at chart time. None claimed this
session — chart-only pass, resolution belongs to a
later session.

**T45 resolved 2026-09-01 by pi**: v1.6 destination
reached. Seven new Layer-1 cases in
`tests/test_tools_in_memory.py` (the five the ticket
enumerated plus `test_t45_concurrent_edit_message_uses_resolved_name`
to pin T39's "error wording references the resolved
name" design call, and `test_t45_standard_412_pointer_omitted_for_empty_name`
to pin the empty-name guard for `list_pages`'s
boundary case). Seven existing byte-for-byte 412
wording tests updated from `==` to `startswith` +
substring checks (matching the T42 / T43 / T44
migration posture). 533 tests pass + 8 skipped
(was 526 + 8 skipped at v1.5 close); `nix flake
check` green.

## Notes

- **Domain**: same as the prior maps (protocol bridge).
  v1.6 stays inside the existing MCP-SB boundary — no
  new transports, no new auth hop, no new dependencies.
- **Skills every session should consult**:
  `mattpocock/skills@grilling`,
  `mattpocock/skills@domain-modeling`,
  `incremental-implementation`. The prior maps'
  standing preferences continue to bind.
- **Standing preferences for this effort** (continuing
  from the prior maps):
  - **No new Python dependencies.** T45 reuses what's
    already in `sb_client.py` / `server.py`. Two
    `str.format` calls and one message-template
    widening — no new code surface beyond the wording
    change.
  - **Wording change is byte-additive, not byte-replacing.**
    The standard 412 message gains a `; read_page({name})
    for the current etag and re-issue` *suffix* — the
    existing `precondition failed; check if_match/
    if_none_match` prefix stays verbatim. The
    silent-overwrite 412 message gains `{name}` and
    `{current_etag}` placeholders that fit naturally
    into the existing sentence structure. An agent that
    pattern-matches on the original substring still
    matches; an agent that pinned the byte-for-byte
    full message (a single test) needs to update.
    This is the same posture the prior maps' T42 /
    T43 / T44 wording changes took.
  - **The bridge does not auto-retry.** v1.6's
    narrow scope is "tell the agent what to do, don't
    do it for them." A wider auto-retry feature
    (the bridge re-reads on a 412 and re-issues the
    write transparently) is a separate ticket — its
    own design surface (idempotency of the
    re-issued write, latency on the happy
    contention path, behavior on concurrent edits
    during the auto-retry window) deserves its own
    charter and out-of-scope conversation.
  - **The T42 `[concurrent_edit_hint: true]` marker
    survives.** T42's contention-threshold hint
    rides as a machine-parseable suffix on the
    standard 412 `ToolError` message. T45's wording
    change sits *before* the marker, so the full
    412 message in a contention window becomes
    `precondition failed; check if_match/if_none_match;
    read_page("Foo.md") for the current etag and re-issue
    [concurrent_edit_hint: true]`. An agent that
    pattern-matches on either the bare prefix or the
    marker substring still matches.
  - **The standard-412 path can't embed the current
    etag.** When SB honors `If-Match` and returns
    412, the response body is empty (per the live
    SB the bridge tested against). The bridge has
    no fresh etag to give — the message instead
    points the agent at `read_page(name)`, which
    will return the current etag. The
    silent-overwrite 412 (T31b) is the path where
    the bridge has the fresh etag in hand (the
    verification re-read just synthesized it); the
    wording embeds it directly.

## Decisions so far

<!-- index only — one line per closed ticket, link to the
ticket's resolution below -->

- [Chart pass, 2026-09-01](#status): v1.6 destination named ("412 retry guidance on the bridge surface"); T45 (enrich both 412 messages with retry guidance and the current etag where the bridge has it in hand) charted with full detail below; the user's wider-scope option (auto-retry on the standard 412 path) was explicitly considered at chart time and ruled out for v1.6 — its design surface (idempotency, latency, concurrent-edit handling during the auto-retry window) deserves its own charter and ADR rather than a bolt-on. Recorded as out-of-scope below; can be revisited in a future map if the enriched messages alone don't close the user's complaint.
- [T45. Enrich 412 messages with retry guidance and the current etag (where the bridge has it)](#t45-enrich-412-messages-with-retry-guidance-and-the-current-etag-where-the-bridge-has-it): both 412 messages widened — standard path gains a `; read_page("<name>") for the current etag and re-issue` suffix (resolved page name, T39's design call); silent-overwrite path embeds the post-write etag directly as `current etag is "<etag>" — re-issue the write with if_match="<etag>"` so the agent has the literal `if_match=` value for the next call without an extra read round trip. Pre-T45 prefixes byte-preserved; only byte-for-byte-pinned tests need updating. T42 contention marker still rides as a trailing suffix. 533 tests pass + 8 skipped; `nix flake check` green.

## Not yet specified

<!-- in-scope fog that can't be ticket-sized yet; graduates as
the frontier advances -->

- **Should the bridge also emit a structured
  `conflict_meta` field on the silent-overwrite
  412?** T42 / T43 / T44 / T45 all ride on the
  message-text channel because the MCP SDK renders
  `ToolError` to `TextContent(text=str(exc))` —
  no native envelope field exists. If a future
  MCP SDK version exposes a `data` or
  `structured_content` field on `ToolError`, T45
  would gain a `{name, expected_etag, current_etag}`
  structured field rather than a substring on the
  message text. Not chartable today (no SDK version
  change in flight); worth flagging as fog so a
  future session that upgrades the SDK revisits.

- **T42's threshold values (N=3, M=60s).** Carried
  over from the v1.5 `## Not yet specified`. The
  constants are at module scope; tuning is a
  one-line change, not a ticket. T45 doesn't
  touch the threshold — the wording change is
  orthogonal to when the hint trips.

## Out of scope

<!-- scope boundaries, not steps on the route; never graduate -->

- [User's wider-scope option (auto-retry on the standard 412 path)](#status): ruled out for v1.6. The auto-retry shape (bridge re-reads on a 412, re-issues the write with the fresh etag transparently) is a real feature, not a one-line fix. Its design surface: idempotency of the re-issued write (what if the agent's body was itself computed from a stale read?); latency on the happy contention path (roughly doubles round-trip time for the common race pattern); behavior on a concurrent edit during the auto-retry window (the second re-read sees another drift — bridge retries again? gives up? surfaces a different error?). These are real questions whose answers deserve their own charter and likely an ADR, not a bolt-on to T45. If the enriched messages alone don't close the user's complaint and the wider-scope option comes back, it's a fresh ticket (probably v1.6's T45a or v1.7's lead ticket) with its own design tree.
- [Adding a structured `conflict_meta` envelope field on the silent-overwrite 412](#not-yet-specified): ruled out for v1.6. The MCP SDK on this build renders `ToolError` to plain `TextContent(text=str(exc))` (verified at `mcp/server/mcpserver/server.py:_handle_call_tool`); no native envelope field exists. T45 rides on the existing message-text channel, matching T42 / T43 / T44's patterns. A future SDK upgrade that exposes `data` on `ToolError` would change the shape, but it's not a v1.6 deliverable.
- [Changing the silent-overwrite 412 to a 200 with a warning field](#not-yet-specified): explicitly considered and rejected. The user's complaint is "the agents don't want to write anymore" — surfacing the conflict as a 200 with a warning would make the agents happier on the first call but leave them silently writing over concurrent edits, which is exactly the failure mode T31b exists to prevent. The bridge must keep raising an error on the silent-overwrite path; the fix is making the error actionable, not making it go away.

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

### T45. Enrich 412 messages with retry guidance and the current etag (where the bridge has it)

> **Labels**: `wayfinder:task`
> **Type**: AFK
> **Assignee**: pi (claimed 2026-09-01, resolved same day)
> **Status**: ✅ resolved 2026-09-01
> **Resolution**: see commit `68eb2`. Both 412 messages
> widened. Standard 412 (`_translate_sb_errors`'s
> `PreconditionFailed` clause) gains a
> `; read_page("<name>") for the current etag and
> re-issue` suffix where `<name>` is the resolved
> page name (T39's design call — error wording
> references the resolved name, not the caller's raw
> input); the empty-name guard skips the pointer for
> `list_pages`'s boundary case. Silent-overwrite 412
> (`_verify_concurrency_token`'s helper) widens
> `_CONCURRENT_EDIT_MSG` from the v1.5
> single-placeholder form (`{expected_etag}`) to a
> three-placeholder form (`{name}`, `{expected_etag}`,
> `{current_etag}`); the bridge embeds the
> post-write etag directly as
> `current etag is "<etag>" — re-issue the write with
> if_match="<etag>"` so the agent has the literal
> `if_match=` value for the next call without an
> extra read round trip. Pre-T45 prefixes
> (`precondition failed` / `concurrent edit detected`)
> byte-preserved; only agents that pinned the
> byte-for-byte full message (a small set of v1.5 /
> earlier tests) need to update. The T42
> `[concurrent_edit_hint: true]` marker rides as the
> trailing suffix on the standard 412 message
> *after* the T45 wording, so a contention-window
> 412 reads: `...; read_page("Foo.md") for the
> current etag and re-issue [concurrent_edit_hint: true]`.
> Seven new Layer-1 cases in
> `tests/test_tools_in_memory.py` (the five the
> ticket enumerated plus
> `test_t45_concurrent_edit_message_uses_resolved_name`
> and
> `test_t45_standard_412_pointer_omitted_for_empty_name`);
> seven existing byte-for-byte 412 wording tests
> updated from `==` to `startswith` + substring
> checks. `README.md` concurrency section gains a
> one-paragraph note ("On a 412, the bridge tells
> you the next call's exact `if_match=` value…
> You don't need to guess."). `docs/design.md`
> § Tools § Status-code mapping 412 row gains a T45
> paragraph documenting both surfaces. `CHANGELOG.md`
> v1.6 `[Unreleased]` entry records the wording
> change with the same migration posture as T42 /
> T43 / T44. 533 tests pass + 8 skipped
> (`nix flake check` green).
> **Question**: How does both 412 paths — the
> standard SB-honors-`If-Match` 412 (raised by
> `_translate_sb_errors`) and the silent-overwrite
> 412 (raised by T31b's `_verify_concurrency_token`)
> — get richer error messages that tell the agent
> exactly what the next call should look like,
> without changing the happy-path wire shape or
> requiring an extra read round trip the agent
> could avoid?
>
> **Context**: The user reports ("There are still
> lots of 412 races. The agents complain about it,
> and don't want to write anymore. Can we at
> least return an error telling them they should
> try again and give them the correct etag or
> something?") that the bridge's 412 error
> surface doesn't help agents recover. Two
> distinct paths produce a 412-shaped error today:
>
> **Path A — standard 412** (SB honors `If-Match`):
> the bridge sends `If-Match: <etag>` on a PUT,
> SB rejects with 412 (empty body). The bridge
> raises `ToolError("precondition failed; check
> if_match/if_none_match")`. The agent doesn't
> know the current etag (SB returned an empty
> body) and has to call `read_page(name)` to find
> out before retrying. The bridge could pre-suggest
> the `read_page(name)` call so the agent doesn't
> have to guess what to do.
>
> **Path B — silent-overwrite 412** (T31b path,
> the one the user is complaining about on the
> W36 page): the bridge sends `If-Match: <etag>`
> on a PUT, SB ignores it and writes anyway (200
> OK). The bridge's `_verify_concurrency_token`
> helper re-reads the page, sees the etag drifted
> from the caller's `if_match`, and raises
> `ToolError("concurrent edit detected: the page
> changed since you read it at {expected_etag};
> read it again and re-issue the write with the
> current etag")`. **The bridge already has the
> new etag from the verification re-read** — it
> just doesn't surface it. The agent has to do
> an extra read to learn what the bridge already
> knows. Closing this gap is the core of T45.
>
> T42 added a `[concurrent_edit_hint: true]`
> marker that fires after N=3 412s on the same
> page within M=60s — the marker survives T45's
> wording change as a suffix, so the contention
> pattern detection isn't broken by the new
> wording.
>
> **Goal**: both 412 paths surface actionable
> retry guidance. The standard path points at
> `read_page(name)`; the silent-overwrite path
> embeds the current etag directly. The agent
> that followed the concurrency protocol
> correctly sees the bridge tell it exactly what
> the next call should look like, without an
> extra read round trip on Path B and without
> having to guess the read tool's name on Path A.
>
> **Done when**:
>
> - `_CONCURRENT_EDIT_MSG` (server.py) gains two
>   new placeholders: `{name}` and `{current_etag}`.
>   New wording:
>   `"concurrent edit detected on {name}: page
>   changed since you read it at {expected_etag};
>   current etag is {current_etag} — re-issue the
>   write with if_match={current_etag}"`. The
>   helper formats with the three values; the
>   agent sees the literal `if_match=` token
>   they can copy-paste.
> - `_verify_concurrency_token` (server.py)
>   formats the message with `name=resolved_name`
>   and `current_etag=post_meta.etag` (the
>   post-write etag from the verification
>   re-read). The `{expected_etag}` placeholder
>   stays as it was. The helper's existing
>   no-op branches (None / "*" expected_etag /
>   dry-run / PageNotFound / transient SB
>   failure) are unchanged.
> - `_translate_sb_errors` (server.py)'s
>   `PreconditionFailed` clause appends a
>   `; read_page({name}) for the current etag
>   and re-issue` suffix to the standard
>   message. The original `precondition failed;
>   check if_match/if_none_match` prefix stays
>   verbatim. The T42 `[concurrent_edit_hint:
>   true]` marker (when the contention threshold
>   trips) sits after the new suffix, so the
>   full 412 message in a contention window is
>   `precondition failed; check if_match/
>   if_none_match; read_page("Foo.md") for the
>   current etag and re-issue
>   [concurrent_edit_hint: true]`. An agent
>   that pattern-matches on the bare
>   `precondition failed` substring still
>   matches; an agent that pattern-matches on
>   `[concurrent_edit_hint:` still matches.
> - The existing byte-for-byte 412 wording
>   tests (currently `assert _text(result) ==
>   "Error executing tool write_page:
>   precondition failed; check if_match/
>   if_none_match"` and analogous for the
>   other 412 tests) update from `==` to
>   `assert "precondition failed; check
>   if_match/if_none_match" in _text(result)` —
>   they assert the original prefix is still
>   present plus the new suffix is present.
>   The migration posture is identical to the
>   T42 / T43 / T44 wording changes the prior
>   maps shipped: existing agents that
>   pattern-match on the bare prefix still
>   match; only the test pinned the byte-for-
>   byte full message.
> - Layer-1 tests:
>   - `test_t45_standard_412_message_includes_read_page_pointer`
>     — a 412 on `write_page("Foo.md", ...,
>     if_match=*)` produces a `ToolError` whose
>     message contains `read_page(` and the
>     page name `Foo.md` literally.
>   - `test_t45_concurrent_edit_message_includes_current_etag`
>     — a silent-overwrite 412 on `write_page(
>     "Foo.md", content="body", if_match='"v1"')`
>     where the post-write re-read returns etag
>     `"new"` produces a `ToolError` whose
>     message contains `current etag is "new"`
>     so the agent has the next `if_match=`
>     value in hand.
>   - `test_t45_concurrent_edit_message_includes_if_match_retry_form`
>     — same shape as above, but the assertion
>     is on the literal `if_match="new"` token
>     being present so the agent's copy-paste
>     surface is one-shot.
>   - `test_t45_concurrent_edit_hint_marker_still_appends`
>     — fires 4 412s on the standard path
>     against the same page in fast succession;
>     the 4th message contains both the new
>     `read_page(` suffix and the T42
>     `[concurrent_edit_hint: true]` marker.
>     Pins that the marker survives the wording
>     change.
>   - `test_t45_concurrent_edit_message_uses_resolved_name`
>     — T39 normalizes bare `Foo` to `Foo.md`;
>     the silent-overwrite 412 message names
>     `Foo.md` (the resolved name), not `Foo`
>     (the caller's input). Matches T39's
>     "error wording references the *resolved*
>     name" design call.
> - `docs/design.md` § Tools § Status-code
>   mapping 412 row gains a T45 paragraph
>   noting the new wording shape (the
>   `read_page({name})` pointer on the
>   standard path; the embedded `{current_etag}`
>   on the silent-overwrite path). The
>   paragraph is a sibling to the existing
>   T31b / T42 / T44 paragraphs so a reader of
>   design.md sees the v1.6 surface described
>   coherently.
> - `README.md` concurrency section gains a
>   one-paragraph note: "On a 412, the bridge
>   tells you the next call's exact
>   `if_match=` value (silent-overwrite path)
>   or the `read_page({name})` call that gives
>   you that value (standard path). You don't
>   need to guess." Sits alongside the existing
>   T44 paragraph so a reader of the README's
>   concurrency section sees the full 412
>   surface in one place.
> - `CHANGELOG.md` v1.6 `[Unreleased]` `###
>   Changed` section gains a T45 entry with
>   the wording shapes, the migration posture
>   (existing agents that pattern-match on the
>   bare prefix still match; only one test
>   pinned the byte-for-byte full message),
>   and a pointer at the four new test cases.
>   The v1.6 status paragraph at the top
>   flips from "T45 on the frontier" to "T45
>   shipped; v1.6 destination reached" once
>   the resolve commit lands.
> - No new dependencies. No SDK version
>   requirement change. No new env vars. The
>   change is local to `_CONCURRENT_EDIT_MSG`,
>   `_verify_concurrency_token`,
>   `_translate_sb_errors`'s `PreconditionFailed`
>   clause, the docs trio (design.md /
>   README.md / CHANGELOG.md), and the
>   `tests/test_tools_in_memory.py` test file.
>
> **Files when resolved**:
> `src/mcp_silverbullet/server.py`
> (`_CONCURRENT_EDIT_MSG` widened;
> `_verify_concurrency_token` formats the new
> placeholders; `_translate_sb_errors`'s 412
> clause appends the `read_page({name})` suffix),
> `tests/test_tools_in_memory.py` (4 new
> Layer-1 cases; 4 existing byte-for-byte 412
> wording tests updated from `==` to `in`),
> `docs/design.md` (§ Tools § Status-code
> mapping 412 row), `README.md` (concurrency
> section), `CHANGELOG.md` (v1.6 entry),
> `docs/wayfinder/map-v1.6.md` (resolution
> entry).
>
> **Blocks on**: nothing. T45 is the lead
> frontier ticket on the v1.6 map.
> **Unblocks**: nothing — terminal ticket.
>
> **Out of scope** (deliberately):
> auto-retry on the standard 412 path
> (ruled out at chart time; the design
> surface deserves its own charter); adding
> a structured `conflict_meta` envelope
> field (no native envelope field exists on
> this SDK build; the prior maps' T42 / T43
> / T44 patterns continue to bind); changing
> the silent-overwrite 412 to a 200 with a
> warning field (the bridge must keep raising
> an error on the silent-overwrite path —
> the fix is making the error actionable,
> not making it go away); tuning T42's
> threshold values (N=3 / M=60s) empirically
> (the constants are at module scope; tuning
> is a one-line change, not a ticket).

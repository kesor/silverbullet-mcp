# Changelog

All notable changes to `mcp-silverbullet` are recorded here. The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
the project adheres to [Semantic Versioning](https://semver.org/).

Versions correspond to the build-map (wayfinder) charts under
`docs/wayfinder/`. The map for an in-flight version lists the open
tickets; this file records what's already shipped.

## [v1.4] — JWT inbound auth + discovery-surface clarity

Build map: [`docs/wayfinder/map-v1.4.md`](docs/wayfinder/map-v1.4.md).
**Status: T37 + T38 shipped 2026-08-31; v1.4 destination reached** —
the inbound auth surface widened from "one shared secret" to a JWT
mode that validates per-user tokens against an IdP's JWKS
(default config targets Cloudflare Access; the v1.x
static-token mode stays available via
`MCP_SILVERBULLET_AUTH_MODE=static`). T37 widens `list_pages`'s
filter surface so an operator who reads "filtered by prefix" as
substring has an explicit `contains=` knob (see Added below);
T38 surfaces the journal gate's purpose and opt-in path more
loudly in the README and `list_pages`'s tool description so an
operator who hits a "no body search" dead end has an obvious
next step.

v1.3 closed on 2026-08-30; v1.4 takes the bridge from
"single-user behind a tunnel" to "per-user authenticated by
whatever IdP the operator wires up". The bridge now exposes
`AccessToken.subject` on every authenticated request (CF
Access user UUID by default; the IdP's `sub` claim for
Auth0/Okta/Google-IAP), ready for future scope-gating and
per-user SB credentials to thread off of.

### Added

- **JWT inbound auth (v1.4 default)** — new
  `:class:`mcp_silverbullet.verifier.JWTVerifier`` that
  validates per-user tokens against the operator's IdP
  JWKS (default config targets Cloudflare Access;
  ``iss`` + ``aud`` + ``exp`` + ``iat`` checked per
  RFC 7519, leeway 30s for clock-skew tolerance,
  ``RS256`` algorithm pinned to refuse the
  algorithm-confusion downgrade attack). The
  successful verification populates
  ``AccessToken.subject`` (CF Access user UUID; the
  IdP's ``sub`` claim for Auth0/Okta/Google-IAP)
  plus the full claim dict on ``AccessToken.claims``
  for downstream per-user work. Five new env vars:
  ``MCP_SILVERBULLET_AUTH_MODE`` (default ``jwt``;
  set to ``static`` for the v1.x surface),
  ``MCP_SILVERBULLET_JWT_ISSUER``, ``_AUDIENCE``,
  ``_JWKS_URL``, ``_ALGORITHMS`` (default
  ``RS256``), ``_LEEWAY_SECONDS`` (default ``30``).
  The ``MCP_SILVERBULLET_TOKEN`` env var is no
  longer required (the bridge logs "ignored in JWT
  mode" at boot if it's still set from a prior
  v1.x upgrade). Backwards-compat:
  ``AUTH_MODE=static`` + ``MCP_SILVERBULLET_TOKEN``
  keeps the v1.x shared-secret surface alive (used
  by ``mcp dev`` CLI sessions). ``build_mcp``
  accepts a ``verifier=`` kwarg (v1.4 production
  path) and a ``token=`` kwarg (v1.x compat); the
  v1.x path goes through a ``_resolve_verifier``
  shim that constructs a ``StaticTokenVerifier``
  from ``token`` if ``verifier`` is unset.
  Migration: existing ``mcp dev`` and
  ``MCP_SILVERBULLET_TOKEN`` setups keep working
  unchanged; production deployments behind
  Cloudflare Access (or any OIDC IdP) get per-user
  ``subject`` for free.

  - **`JWTVerifier`** —
    ``src/mcp_silverbullet/verifier.py``.
    PyJWKClient-backed key cache (5-min default
    lifespan; auto-refetched on cache miss);
    ``algorithms=`` allow-list (default
    ``("RS256",)``); ``issuer=``, ``audience=``,
    ``leeway_seconds=``, ``required_claims=``
    constructor knobs for IdP variations.
  - **`select_verifier`** — factory that maps the
    ``MCP_SILVERBULLET_AUTH_MODE`` env contract to
    a verifier instance, fails loud on missing
    required fields.
  - **`build_verifier`** —
    ``src/mcp_silverbullet/main.py``. Thin wrapper
    over ``select_verifier`` that handles the
    ``None``-vs-empty-string distinction
    ``load_settings`` produces.

- **T37 `list_pages` substring filter** — new
  `contains: str = ""` parameter on `list_pages`
  that does substring matching against the page
  name (alongside the existing `prefix=` filter
  which keeps v1's `startswith` semantics). The two
  filters compose as AND when both are set; either
  empty is a no-op for that criterion; both empty
  returns the full listing. Both filters run
  client-side before per-page hydration, so a
  narrow filter reduces the N+1 round-trip count
  the same way `prefix=` does. Wire surface: the
  v1 / v1.1 / v1.2 / v1.3 `prefix=` semantics are
  unchanged for callers that already use the
  parameter; the new `contains=` parameter is purely
  additive. Closes the bug-report surface where an
  operator reads "filtered by prefix" as substring
  and now has an explicit knob.
- **T38 journal-gate discovery pointer** — the
  README's `### Optional: journal surface` section
  is renamed to `### Discovery tools (journal-gated)`
  and reframed around the three discovery tools
  (`pages_touching_topic`, `search_pages`,
  `find_backlinks`), with the two env vars that
  unlock them (`MCP_SILVERBULLET_SPACE_PATH` +
  `MCP_SILVERBULLET_JOURNAL_TOOLS=1`) named in the
  preamble so an operator who hits a "no body
  search" dead end has an obvious next step rather
  than relitigating the scope. The `list_pages`
  tool description gains a one-sentence pointer at
  the same gate (matching the charter exactly:
  "Body-content search lives behind the journal gate
  (`MCP_SILVERBULLET_JOURNAL_TOOLS=1` +
  `MCP_SILVERBULLET_SPACE_PATH`); this filter only
  ever matches against page names."). The env-var
  table rows for both gate vars gain a parenthetical
  pointing at the new section. `docs/design.md`
  § What we are not doing gains a dedicated
  bullet on body-substring search without the
  gate: SB exposes no HTTP search endpoint, the
  bridge has no way to substring-search page bodies
  without filesystem access, and the gate is the
  only path. Layer-1 test
  `test_t38_list_pages_description_points_at_journal_gate`
  pins the description's gate pointer. No behavior
  change; no wire-shape change; the journal gate
  stays exactly as it was (the surface the bug
  reporter wanted was already shipped — T38 makes
  it discoverable).

## [Unreleased] — v1.5 (agent-experience hardening)

Build map: [`docs/wayfinder/map-v1.5.md`](docs/wayfinder/map-v1.5.md).
**Status: T39 + T40 + T41 + T42 + T43 + T44 shipped
2026-08-31; v1.5 destination reached** — the
`.md`-suffix split closes via T39's
name-normalization helper (threaded into every
`name`-taking tool), with a `name_resolution`
envelope field that teaches the agent the
convention for its next call. T40 lifts the
upfront empty-input guard (already shipped on
`create_page` / `append_to_page` /
`prepend_to_page` / `patch_page_replace` /
`check_task`) into two shared helpers and threads
them into the four tools that didn't have it yet
(`write_page` / `delete_page` / `move_page`'s source
and destination / `patch_page_lines`). T41 lands
three small doc clarifications on the tool surface
and the bridge's `MCPServer.instructions` block (see
Added below). T42 surfaces a contention-window
signal on the 412 error envelope after N=3 412s on
the same page within M=60s. T44 fixes T31b's
false-positive "concurrent edit detected" on every
successful write by dropping the mtime component
from the synthesized etag (see Changed below for
the wire-shape migration note). T43 surfaces a
`[cf_hint: {...}]` marker on the 5xx error
envelope when the upstream body is CF-shaped
(carries `cloudflare_error` / `error_category` /
`ray_id`); the marker is a JSON-serialized dict
of `retry_after` / `error_code` / `title` that
an agent can `json.loads` directly to decide
whether to retry without pattern-matching the
raw CF JSON body (see Added below). The marker
is conditional on a CF-shaped body — non-CF 5xx
bodies leave the pre-T43 wording unchanged,
so non-CF deployments see no behavior change.

T39 is **behavior-changing** for v1 / v1.1 / v1.2 / v1.3 callers
that explicitly pass `name="Foo"` (bare) expecting a 500-shaped
error: the bridge now resolves to `Foo.md` and returns 200 with
the body of `Foo.md`. Any caller that was already retrying with
`.md` after a 500 sees no behavior change. The
`name_resolution` envelope makes the convention explicit so the
agent learns the convention rather than continuing to retype.

### Added

- **T39 name normalization** — new
  `_normalize_page_name(name)` helper in
  `src/mcp_silverbullet/server.py`. Pure,
  idempotent, surface-level invisible: strips
  leading/trailing whitespace, appends `.md` to
  names whose basename has no `.` (so `Foo` →
  `Foo.md`, `Areas/Foo` → `Areas/Foo.md`,
  `Foo.txt` stays `Foo.txt`, `Foo.tar.gz` stays
  `Foo.tar.gz`, `.gitignore` stays `.gitignore`).
  Threaded into every `name`-taking tool handler
  at the top, before the SB round trip and
  before `_check_body_size`: `read_page`,
  `page_exists`, `write_page`, `create_page`,
  `delete_page`, `append_to_page`,
  `prepend_to_page`, `patch_page_lines`,
  `patch_page_replace`, `move_page` (both
  `name` and `new_name`), `diff_pages` (both
  `name` and `other_name`), `check_task`
  (`page` only — `ref` is a wikilink target,
  handled by the existing
  `_normalize_link_target` canonicalization),
  `list_tasks` (per-page form's `page`), the
  `silverbullet://page/{name}` resource
  template, and the `_hydrate_list_etags`
  walker (so list rows with bare names hydrate
  successfully against the canonical file).

- **T39 `name_resolution` envelope field** —
  new `_name_resolution_payload(requested,
  resolved)` helper that returns a dict like
  `{"name_resolution": {"requested": "Foo",
  "resolved": "Foo.md", "suffix_added":
  ".md"}}` when the caller's input was
  normalized, and an empty dict when it was
  already canonical. Attached to the success
  envelope of every tool that touches a `name`
  (writes, reads, diffs, the resource
  template); the field is **conditional** —
  omitted when the caller's input was already
  canonical, so existing wire-shape assertions
  on the success envelope continue to pass
  byte-for-byte. The field teaches the agent
  the convention: a caller that passes `"Foo"`
  and gets back `name_resolution.suffix_added:
  ".md"` learns to pass `Foo.md` on its next
  call.

- **T40 lift upfront empty-input validation
  across write tools** — two module-scope
  helpers in `src/mcp_silverbullet/server.py`:

  - `_validate_nonempty_name(name)` raises
    `ToolError("name must not be empty")` when
    the caller's `name` is empty or
    whitespace-only.
  - `_validate_nonempty_value(value, *, label)`
    raises
    `ToolError("<label> must not be empty")`
    with the parameter name inlined, for
    body-shaped inputs whose parameter name
    varies by tool.

  Threaded into every write tool at the top of
  each handler, **before** T39's name
  normalization (so a caller passing `name=""`
  still sees the loud empty-name error rather
  than the normalized form `".md"` silently
  succeeding):

  - `write_page` — both `name` and `content`
    guards. `name=""` raises
    `ToolError("name must not be empty")`;
    `content=""` raises
    `ToolError("content must not be empty")`.
  - `delete_page` — `name` guard.
    `name=""` raises
    `ToolError("name must not be empty")`.
  - `move_page` — both `name` and `new_name`
    guards (same helper, same wording).
  - `patch_page_lines` — `name` guard.
  - `patch_page_replace` — already had the
    `find` guard; threaded through the helper
    for consistency. The `new_string=""` case
    is **deliberately not** guarded — it's the
    documented "delete the match" path
    (`"abcdefg".replace("cd", "")` is
    `"abefg"`), not a caller bug. T40's
    ticket originally proposed guarding it,
    but the documented delete-match surface
    takes priority; this is now a
    `## Drive-by` note rather than an open
    ticket.

  The five already-guarded tools
  (`create_page` / `append_to_page` /
  `prepend_to_page` / `patch_page_replace` /
  `check_task`) had their inline guards
  replaced with the shared helpers so the
  wording matches across every tool. Existing
  test cases continue to pass byte-for-byte;
  17 new Layer-1 cases in
  `tests/test_tools_in_memory.py` lock the
  T40 surface (empty + whitespace-only
  inputs across every tool, no-SB-round-trip
  invariant, normalization-runs-after-empty-
  guard ordering, and a test pinning the
  `patch_page_replace`'s `new_string=""`
  "delete match" path against an accidental
  future guard). Tool descriptions in
  `server.py` and `docs/design.md` § Tools
  table rows updated to mention the new
  guards.

- **T41 doc clarifications on the tool
  surface** — three small, sharp doc
  additions, no new code beyond the
  description strings:

  - `read_page`'s description gains a
    sentence noting that pages containing
    Space Lua template syntax (e.g.
    `${template.each(...)}`) are returned
    as raw markdown source, never as
    rendered output — the bridge is a
    transport, not a renderer
    (`b5`). Before: an agent reading the
    page for the first time saw
    "broken" template syntax and
    sometimes reported it as a bug.
    After: the description tells the
    agent what to expect.
  - `move_page`'s description gains a
    sentence noting that the `name ==
    new_name` no-op never raises 412 even
    when the caller passes
    `if_match=<stale_etag>` and the page
    has drifted (`b8`). Before: the
    description said the no-op ignores
    `if_match` but didn't say it never
    raises 412 — a caller passing
    `if_match=<expected_etag>` on a
    no-op might wait for a 412 that will
    never come. After: the description
    makes the silent no-op contract
    explicit.
  - `MCPServer.instructions` gains a
    single sentence noting the `.md`-
    suffix convention lifted by T39:
    "Page names passed to any `name`-
    taking tool without a file extension
    are automatically suffixed with
    `.md` (T39); names with an existing
    extension (`Foo.txt`) pass through
    unchanged." Before: an agent that
    connected for the first time didn't
    know about the convention until its
    first successful response. After:
    the convention is in the
    system-prompt-ish text the agent
    reads on connect.

  Three new Layer-1 cases in
  `tests/test_tools_in_memory.py` lock
  the T41 surface (one per addition).
  `docs/design.md` § Tools table rows
  for `read_page` and `move_page`
  updated to mention the T41 sentence
  in the Side-effects column. No
  behavior change; no wire-shape change;
  no new dependencies.

- **T42 412 contention hint** — when
  the bridge raises the unified 412
  `ToolError("precondition failed;
  check if_match/if_none_match")` on
  the same page N=3 times within
  M=60 seconds, the *next* 412 on
  that page appends
  ` [concurrent_edit_hint: true]` to
  the message so an agent stuck in a
  contention loop (the bug reporter's
  W36 pattern: SB UI racing a second
  editor every few minutes) gets a
  clear signal to back off rather than
  pattern-matching bare
  `precondition failed` strings. New
  module-scope helper
  `_contention_hint(name)` in
  `src/mcp_silverbullet/server.py`
  (per-name sliding-window counter
  keyed on `(name,)`, bounded at
  `_CONTENTION_THRESHOLD` entries per
  deque; trip-on-next semantics so the
  4th 412 is the first one carrying the
  marker). Threaded into the
  `PreconditionFailed` clause of
  `_translate_sb_errors`. The hint is
  **advisory, never authoritative** —
  the bridge still raises the standard
  412 `ToolError` regardless; agents
  that don't check for the marker see
  no change. The marker only appears
  on the error path; a successful
  write after three 412s returns the
  T23 ack envelope with no hint field.
  Constants at module scope
  (`_CONTENTION_WINDOW_SECONDS=60`,
  `_CONTENTION_THRESHOLD=3`) so
  future tuning is a one-line change,
  not a ticket. Four new Layer-1 cases
  in
  `tests/test_tools_in_memory.py`
  lock the surface: 4 consecutive
  412s on the same page (the 4th
  carries the marker; the first 3
  don't); 1 412 each on two different
  pages (neither carries the marker
  — the counter is per-page); a 60s
  window jump after 3 412s evicts the
  old timestamps (the next 412 carries
  no marker); a successful write
  after three 412s returns the T23
  ack envelope with no marker on the
  success path. An autouse fixture in
  the test file resets the per-process
  contention counter between tests
  so test isolation is preserved (a
  process-global state that drifted
  across tests would otherwise pollute
  tests asserting the bare 412
  wording). No new dependencies; no
  behavior change for the
  happy-path single-412 case (the
  hint only fires after the threshold
  trips).

- **T43 CF 5xx `cf_hint` envelope** — when the
  bridge raises a 5xx ``ToolError`` against a
  CF-fronted SB whose response body is a
  Cloudflare-shaped JSON envelope (carries
  ``cloudflare_error`` / ``error_category`` /
  ``ray_id``), the error message gains a
  `` [cf_hint: {...}]`` suffix carrying the
  parsed ``retry_after`` (seconds), ``error_code``
  (the numeric CF code, e.g. ``502``), and
  ``title`` (the human-readable summary). New
  module-scope helper
  `_parse_cf_error(body: str | None) -> dict |
  None` in `src/mcp_silverbullet/sb_client.py`
  — returns ``None`` for non-CF bodies (empty,
  plain text, random JSON, or JSON without the
  CF marker fields), else returns the
  three-field hint dict. New ``cf_hint: dict |
  None = None`` attribute on
  :class:`mcp_silverbullet.sb_client.ServerError`;
  populated by ``_raise_for_status`` on any 5xx
  response (and the catch-all 4xx that folds to
  ``ServerError``). Threaded into the
  ``ServerError`` clause of
  ``_translate_sb_errors`` in ``server.py``;
  the marker rides on the message-text channel
  (``TextContent(text=str(exc))``) using
  ``json.dumps`` so an agent can ``json.loads``
  the suffix directly — same envelope-shape
  design call as T42's
  ``concurrent_edit_hint`` (no native envelope
  field exists on the MCP wire). The hint is
  **conditional** — ``None`` ``cf_hint`` leaves
  the pre-T43 wording byte-for-byte unchanged,
  so non-CF deployments see no behavior change.
  Six new Layer-1 cases in
  ``tests/test_tools_in_memory.py`` (CF body →
  marker present, non-CF body → no marker,
  random JSON body → no marker, empty body →
  no marker, CF body without ``retry_after`` →
  ``retry_after: None`` in marker, success
  path after 5xx → no marker on the T23 ack)
  and eleven new Layer-3 cases in
  ``tests/test_sb_client.py`` (CF body →
  three fields, empty body → ``None``, non-JSON
  → ``None``, random JSON → ``None``, CF body
  without ``retry_after`` → ``retry_after:
  None``, string ``error_code`` coercion,
  top-level non-object JSON → ``None``, 5xx
  populates ``cf_hint`` on ``ServerError``,
  non-CF 5xx leaves ``cf_hint=None``, empty
  5xx body leaves ``cf_hint=None``, every
  entry point that flows through
  ``_raise_for_status`` threads the hint).
  `docs/design.md` § Tools § Status-code
  mapping 5xx row gains a `cf_hint` field
  note documenting the marker shape, the
  three surfaced fields, the conditional
  behavior, and the rationale for the
  message-text-channel pattern. No new
  dependencies; no behavior change for
  non-CF 5xx; no wire-shape change for the
  success path; the agent's hint-extraction
  is opt-in (a caller that doesn't
  ``json.loads`` the suffix sees no change).

### Changed

- **T44 synthesized-etag format change** — the
  `synthesize_etag` fallback (used when SB strips
  `ETag` from the response, as it does on this dev
  box) used to return `"{last_modified_ms}-{size_bytes}"`
  and now returns `"{size_bytes}"`. The mtime
  component was dropped because the bridge stamps
  `X-Last-Modified` with `now_ms` on every PUT
  request (`_WRITE_HEADERS`), which made the dashed
  form drift on every write even when the body was
  unchanged; the drift made T31b's post-write
  verification raise `ToolError("concurrent edit
  detected: …")` on every successful write where
  the write actually succeeded (the bridge was
  lying about 412). The size-only primitive is
  what the concurrency check actually needs:
  same body → same size → same etag → no drift;
  different body → different size → different
  etag → drift. **Wire-shape change** (backwards-
  incompatible for v1.3 / v1.4 callers): any caller
  holding a synthesized etag across calls should
  re-read once after this fix lands to pick up the
  new canonical form. Real `ETag` headers (from SB
  builds that emit them) are unaffected. Three
  `tests/test_sb_client.py` cases updated to assert
  the new form; two `tests/test_tools_in_memory.py`
  T31b tests already passed against the new form
  without modification (their existing mocks
  happened to match). One new Layer-1 test
  (`test_t31b_write_page_verification_passes_when_synthesized_etag_unchanged`)
  locks the realistic-shape happy path that the
  pre-T44 false-positive broke. `tests/test_e2e_live_sb.py`
  updated to construct the new form on its
  fallback branches and assert the new wire shape
  on the live-SB drift test. Real `ETag` headers
  (the v1.3 contract path on SBs that emit them)
  are untouched.

## [Unreleased] — v1.6 (412 retry guidance on the bridge surface)

Build map: [`docs/wayfinder/map-v1.6.md`](docs/wayfinder/map-v1.6.md).
**Status: T45 shipped 2026-09-01; v1.6 destination reached**. v1.5
shipped the contention-window signal (T42) and the synthesized-etag
fix (T44) but didn't address the underlying user complaint:
agents in 412-contention loops don't know what to do next.
v1.6 closes that gap by widening both 412 messages with
actionable retry guidance so an agent that followed the
concurrency protocol correctly sees the bridge tell it the next
call's exact shape, without an extra read round trip on the
silent-overwrite path and without having to guess the read
tool's name on the standard path.

### Changed

- **T45 standard 412 wording gains `read_page(<name>)`
  pointer** — the `_translate_sb_errors` `PreconditionFailed`
  clause now appends `; read_page("<name>") for the current
  etag and re-issue` to the standard 412 `ToolError` message.
  The `<name>` is the resolved page name (T39's design call),
  so an agent that called `write_page("Foo")` sees
  `read_page("Foo.md")` in the error — a copy-paste-able
  read tool call. The pre-T45 prefix `precondition failed;
  check if_match/if_none_match` is byte-preserved: an agent
  that pattern-matches on the bare prefix still matches; only
  agents that pinned the byte-for-byte full message (a small
  set of v1.5 / earlier tests) need to update. The standard
  path can't embed the current etag directly (SB's 412
  response body is empty on this build), so the wording
  points at the read instead. The `list_pages` 412 has no
  per-page name — the empty-name guard skips the pointer and
  the bare prefix is enough. The T42 `[concurrent_edit_hint:
  true]` contention marker rides as the trailing suffix *after*
  the T45 wording, so a contention-window 412 reads:
  `...; read_page("Foo.md") for the current etag and re-issue
  [concurrent_edit_hint: true]`. An agent that pattern-matches
  on either the bare prefix or the marker substring still
  matches.

- **T45 silent-overwrite 412 wording embeds the current
  etag** — the `_CONCURRENT_EDIT_MSG` template (the
  `ToolError` raised by `_verify_concurrency_token` on
  SBs that don't honor `If-Match`) widens from the v1.5
  single-placeholder form (`{expected_etag}`) to a
  three-placeholder form (`{name}`, `{expected_etag}`,
  `{current_etag}`). The bridge has the post-write etag in
  hand from the verification re-read, and the wording
  embeds it directly as
  `current etag is "<etag>" — re-issue the write with
  if_match="<etag>"`, so the agent has the literal
  `if_match=` value for the next call without an extra
  read round trip. The pre-T45 prefix
  `concurrent edit detected` is byte-preserved: an agent
  that pattern-matches on the bare prefix still matches;
  only agents that pinned the byte-for-byte full message
  (a small set of T31b tests) need to update. `name` is
  the resolved page name (matching T39's design call —
  error wording references the resolved name, not the
  caller's raw input); `current_etag` falls back to
  `"None"` when SB stripped the `ETag` header and the
  synthesized-etag primitive returned `None` (the rare
  case; on this dev box the synthesized form is
  `"{size_bytes}"` and is always populated). No new
  dependencies; no SDK version change; no new env vars.
  Local to `_CONCURRENT_EDIT_MSG`,
  `_verify_concurrency_token`, `_translate_sb_errors`'s
  412 clause, the docs trio (`docs/design.md` /
  `README.md` / `CHANGELOG.md`), and
  `tests/test_tools_in_memory.py`. Seven new Layer-1 test
  cases (the five the ticket enumerated plus
  `test_t45_concurrent_edit_message_uses_resolved_name`
  and `test_t45_standard_412_pointer_omitted_for_empty_name`
  to pin the resolved-name and empty-name guards);
  seven existing byte-for-byte 412 wording tests updated
  from `==` to `startswith` + substring checks (matching
  the T42 / T43 / T44 migration posture — only the test
  pinned the full byte-for-byte message; agents that
  pattern-match on the bare prefix are unaffected).

## [v1.3] — agent-grade discovery + edit hygiene

Build map: [`docs/wayfinder/map-v1.3.md`](docs/wayfinder/map-v1.3.md).
**Status: T31 / T31a / T31b / T32 / T33 / T34 / T35 / T36
shipped (2026-08-30); v1.3 destination reached** — T31 closed
**negatively** on 2026-08-30, prompting the T31a + T31b follow-ups
that are now landed. All eight v1.3 tickets are closed; the
bridge exposes fourteen tools plus one resource template (the
v1.2 baseline of twelve plus `create_page` and `prepend_to_page`).

The live SB on this dev box (`127.0.0.1:63000`, the build v1
T6 / v1.1 T22 / v1.2 T7 / T30 all ran against) does NOT
honor `If-Match` on `PUT /.fs/{name}` (the second write in
the T31 verification silently overwrote the page with
`is_error=False`) AND does NOT return an `ETag` response
header on PUT (every `write_page` envelope on this SB had
`etag=null` pre-T31a). Two separate SB facts, both fatal to
the v1.2 `If-Match` story. The bridge's plumbing is correct
(T31's test proved `If-Match` reaches SB verbatim); what's
missing is the SB-side contract. T31a + T31b fix the
concurrency story at the bridge layer: synthetic-etag fallback
gives the bridge something to thread into `If-Match` when SB
strips `ETag`; post-write verification detects the stale-etag
overwrite when SB returns 200 instead of 412.

**Ticket split** (post-T31 pivot):

- **T31a** — **SHIPPED 2026-08-30**. Synthetic-etag fallback
  in `sb_client.py`. New `synthesize_etag(last_modified_ms,
  size_bytes)` helper; `_etag_from_response` calls it when
  SB returns no `ETag`. Format `"{ms}-{bytes}"` (or `"{ms}"`
  alone when only the timestamp header is populated; `None`
  when both are stripped).
- **T31b** — **SHIPPED 2026-08-30**. Post-write concurrency
  verification helper in `server.py`. New
  `_verify_concurrency_token(sb_client, name,
  post_write_meta, expected_etag, dry_run=False)` re-reads
  the page after a successful PUT and raises
  `ToolError("concurrent edit detected: …")` when the
  post-write etag differs from the caller's `if_match`.
  Threaded into `write_page`, `append_to_page`,
  `patch_page_lines`, `patch_page_replace`, `move_page`,
  `check_task`, `delete_page`. The 412 path remains primary
  on SBs that honor `If-Match`; the helper is the fallback
  for SBs that don't.
- **T32** — `create_page` tool. **SHIPPED 2026-08-30** (see
  `### Added`).
- **T33** — `prepend_to_page` tool. **SHIPPED 2026-08-30** (see
  `### Added`).
- **T34** — `search_pages` journal-gated discovery tool.
  **SHIPPED 2026-08-30** (see `### Added`).
- **T35** — `find_backlinks` journal-gated discovery tool.
  **SHIPPED 2026-08-30** (see `### Added`).
- **T36** — 256 KiB body-size cap. **SHIPPED 2026-08-30**
  (see `### Added`).

See [`docs/wayfinder/map-v1.3.md`](docs/wayfinder/map-v1.3.md)
for the full T31 resolution + the T31a / T31b charter. All
v1.3 tickets (T31a / T31b / T32 / T33 / T34 / T35 / T36)
shipped on 2026-08-30 (see `### Added`); the v1.3
destination is reached.

### Added

- **Synthetic-etag fallback when SB strips `ETag`** (T31a) —
  new `synthesize_etag(last_modified_ms, size_bytes)` helper
  in `sb_client.py`; `_etag_from_response` (extracted from
  `_meta_from_response`) calls it when SB's response has no
  `ETag` header. Format: `"{last_modified_ms}-{size_bytes}"`
  when both `X-Last-Modified` and `X-Content-Length` are
  populated (the normal case on this SB build);
  `"{last_modified_ms}"` alone when only the timestamp is
  populated; `None` when both are stripped. The wire shape
  is unchanged from the caller's perspective — `etag` is
  still a string (or `null`); the bridge just has *some*
  value to thread into `If-Match` on SBs that strip
  `ETag`. Out of scope for `list_pages` (T28 already
  documented `etag=null` on this SB build for list rows;
  changing it would alter list behavior on SBs that omit
  etags everywhere — `list_pages_hydrate_etags` already
  exists for the per-page-fetch opt-in).

  Migration: no caller action needed. The fallback is
  invisible — a caller that threads `read.etag` into
  `write(if_match=…)` gets the same flow on SBs that emit
  a real `ETag` and on SBs that strip it; the envelope
  shape is the same. The only difference is the wire-level
  `If-Match` value sent to SB, which callers never see.
- **Post-write concurrency verification on SBs that don't
  honor `If-Match`** (T31b) — new
  `_verify_concurrency_token(sb_client, name,
  post_write_meta, expected_etag, dry_run=False)` helper
  in `server.py`. After a successful 200 write that
  threaded an etag through `if_match`, the helper
  re-reads the page (`read_page_meta`, body skipped for
  cheapness) and compares the post-write etag against
  the one the caller passed. A mismatch raises
  `ToolError("concurrent edit detected: the page
  changed since you read it at {expected_etag}; read it
  again and re-issue the write with the current etag")`.

  Threaded into every write tool that takes a concrete
  `if_match`: `write_page`, `append_to_page`,
  `patch_page_lines`, `patch_page_replace`, `move_page`
  (post-delete, lightweight — re-read 404s, no-op),
  `check_task`, `delete_page` (same lightweight shape as
  `move_page`). Read-modify-write tools now auto-thread
  the read's etag into the write's `if_match` when the
  caller passes `if_match=None`, so a concurrent edit
  between read and write surfaces as the unified
  concurrency error even without the caller managing an
  etag round-trip.

  The existing 412 path remains primary on SBs that
  honor `If-Match` (cheaper, fires before the helper
  runs); the helper is the fallback for SBs that don't
  (which is this dev box, per T31's negative resolution).
  `if_match="*"` and `if_match=None` opt out of the
  verification (no value to compare against); `dry_run=True`
  opts out (no write happened to verify). Re-read 404
  (page deleted out-of-band post-write) and 5xx
  (transient SB failure) both degrade gracefully — no
  false-positive concurrency errors.

  Migration: no caller action needed on the happy path.
  Callers that previously saw silent overwrites on
  writes with stale etags now see a clear `ToolError`
  they can recover from with a fresh read + retry.
  Callers that want to bypass the verification can pass
  `if_match=None` (the helper no-ops) — but the v1.3
  recommended pattern is to leave the auto-thread
  enabled and let the helper handle races.
- **`search_pages(query, prefix?, limit=20)`** (T34) — bounded
  substring content search over the SB space directory.
  Thin wrapper over the v1 `pages_touching_topic` machinery
  (T12) that applies a `limit` knob (default 20, hard cap 100)
  on top of T12's name-ascending sort. Returns the same
  `{name, match, snippet}[]` wire shape: `match` is `"name"`,
  `"content"`, or `"both"`; `snippet` is an ~80-char window
  centered on the body match (or a body excerpt for name-only
  matches). Restricted to pages whose relative path contains
  `prefix`. Journal-gated (requires
  `MCP_SILVERBULLET_JOURNAL_TOOLS=1` and
  `MCP_SILVERBULLET_SPACE_PATH`); without the gate the tool
  is not registered and the bridge boots cleanly.

  Errors: empty / whitespace-only `query` →
  `ToolError("query must not be empty")` before any FS walk;
  `limit < 1` → `ToolError("limit must be a positive integer;
  got {limit}")`; `limit > 100` → `ToolError("limit {limit}
  exceeds hard cap of 100; narrow the query or prefix instead
  of raising the cap")`; `prefix` carries the same traversal
  guard (`..` and absolute paths rejected) the rest of the
  journal surface uses. `rg --json` / pure-Python fallback
  inherits from T12 unchanged.

  Migration: replace
  `pages_touching_topic(query, prefix=…)` with
  `search_pages(query, prefix=…)` when the caller wants a
  bounded result list (the default v1.3 agent pattern). The
  unbounded `pages_touching_topic` stays available for the
  rarer "scan everything" case.
- **`find_backlinks(target) -> [{file, line, text}]`** (T35) —
  wikilink-target backlinks. Walks the SB space directory,
  scans every `*.md` for `[[target]]` / `[[target|alias]]`
  references; returns one entry per matching line.
  `{file, line, text}` wire shape (closest reference input:
  `lidiaev/me-db`'s `find_backlinks`). `line` is 1-indexed
  (matching `patch_page_lines` / editor conventions);
  `text` is the stripped line content; `file` is the
  relative path to the linking page. Target normalization:
  leading/trailing slashes and a trailing `.md` are stripped
  before matching, so `Projects/Foo`, `Projects/Foo.md`, and
  `/Projects/Foo/` all match the same canonical target. The
  alias (`|alias` suffix on `[[target|alias]]`) is stripped
  before matching — the alias is the *display* text, not a
  different page. Self-links are returned (filter
  client-side); multiple references on one line collapse to
  one entry (per-line granularity, matching the T12
  snippet shape). Empty / whitespace-only `target` raises
  `ToolError("target must not be empty")` upfront, before
  any FS walk. No matches returns `[]`, not a `ToolError`
  (the agent might be querying pre-emptively for a
  rename-pre-flight check). The walker reuses T11/T12's
  `_iter_md` (hidden-directory skip); unreadable pages
  (binary content, permissions error) are skipped silently
  so a single bad page doesn't abort the scan.

  Migration: an agent that currently calls
  `pages_touching_topic("[[Projects/Foo]]")` and pattern-
  matches the result itself to find references now has a
  first-class `find_backlinks(target)` tool. The new tool
  is the rename-pre-flight workflow the T35 ticket was
  chartered for: before renaming a page, the agent lists
  every backlink to it (the lines that would need
  `[[old_name]]` → `[[new_name]]` rewrites); the bridge
  doesn't rewrite links on rename (that's a separate
  `move_page`-related concern, also out of scope for v1.3
  per the standing preference).
- **`create_page(name, content)`** (T32) — refuse-to-overwrite
  create tool, distinct from `write_page`'s overwrite-or-create
  default. Thin wrapper over `write_page(if_match="*")` that
  translates the 412 path into a clean
  `ToolError("page already exists: {name}; use write_page to
  overwrite")`. Same T23 ack envelope return shape as
  `write_page`, so an agent that learns one shape has it for
  both tools. `if_match="*"` is implied (no caller-controlled
  precondition — agents that want write-with-precondition
  call `write_page` directly; exposing `if_match` on
  `create_page` would be a misuse-of-API footgun). Empty /
  whitespace-only `name` raises `ToolError("name must not be
  empty")` upfront, before any SB round trip. Other SB error
  types (404 / 5xx / timeout) bubble through the standard
  `_translate_sb_errors` path for the unified
  `page not found: {name}` / `silverbullet error: {status}`
  wording. The `if_match="*"` path opts out of T31b's
  post-write verification helper per the helper's contract
  (`expected_etag == "*": return`).

  Documented limitation: on SBs that don't honor
  `If-Match` (T31's negative finding on this dev box),
  `create_page` silently overwrites an existing page —
  the `If-Match: *` precondition isn't enforced at the SB
  layer, so the bridge can't distinguish a successful
  create from a successful overwrite. The honest wire
  shape is one that maps cleanly to SBs that *do* honor
  `If-Match`; the silent-overwrite case is a documented
  limitation, not a hidden bug. A `T32a` follow-up could
  close the gap with an `exists_page` round trip before
  the PUT (extra cost on the happy path for a rare edge
  case) or with a synthesized-etag precondition + T31b
  verification (relies on T31a's synthetic-etag fallback
  but doesn't fit `if_match="*"`'s "no etag to compare"
  semantics cleanly). The T32 charter is the 412 →
  `already_exists` translation only.

  Migration: an agent that currently does the
  three-step recipe (`page_exists` → `write_page(name,
  content, if_match="*")` → handle-412-if-it-fired) now
  has a one-step `create_page` tool with a clear
  `already_exists` error message that names the right
  next tool. The existing `page_exists` /
  `write_page(if_match="*")` flow stays available — the
  agent that wants explicit pre-flight checking (e.g.,
  to log the existence decision before deciding to
  create) keeps the v1.2 surface.
- **`prepend_to_page(name, content,
  position="after_frontmatter"|"top", if_match?,
  dry_run=False)`** (T33) — top-of-body insert with YAML
  frontmatter awareness. Mirrors `append_to_page`'s
  read-modify-write + `dry_run` shape but inserts at
  the top. Default `position="after_frontmatter"` —
  inserts the new content *between* the closing `---`
  of the frontmatter block and the first body line
  (the human-meaningful default for journal /
  daily-notes pages with YAML frontmatter; the new
  content lands at the top of the *body*, not above
  the frontmatter). `position="top"` overrides for the
  rare absolute-top intent. For pages without
  frontmatter, both positions produce the same splice
  (new content at the absolute top).

  Frontmatter detection: a leading `---\n…\n---\n`
  block (LF). A page that opens with `---` but doesn't
  close it (a malformed frontmatter block) is treated
  as no-frontmatter — the new content lands at the
  absolute top, same as a page with no frontmatter at
  all. The same "raw text, no parser" pattern the
  rest of the bridge uses; no YAML library is pulled
  in. The new `_split_frontmatter_block` helper lives
  in `server.py` (module-private; not exported) and
  returns `(frontmatter_str_or_None, rest_str)` where
  `None` is the canonical "no frontmatter" signal.

  Concurrency story inherits from T31b: `if_match=None`
  auto-threads the read's etag into the write's
  precondition (a concurrent edit between read and
  write surfaces as `ToolError("concurrent edit
  detected: …")` via the T31b helper, even without the
  caller managing an etag round-trip). Explicit
  `if_match=<etag>` from the caller wins verbatim.
  `dry_run=True` returns the T26 `{dry_run, original,
  patched, diff}` preview without writing (the read
  still happens, `if_match` is validated against the
  read's etag, T31b's helper no-ops per its
  `dry_run=True` short-circuit).

  Errors: empty / whitespace-only `content` upfront
  `ToolError("content must not be empty")`; unknown
  `position` upfront `ToolError("position must be one
  of: after_frontmatter, top")`; 412 on stale
  `if_match` (standard wording); 404 on missing page
  (standard wording); 413 on a body > 4 MiB (standard
  wording). Returns the T23 ack envelope on success.

  Migration: an agent that currently does the
  five-step recipe (`read_page` → manually split the
  frontmatter from the body → splice the new content
  above the body → re-attach the frontmatter → write
  it back) now has a one-step `prepend_to_page` tool
  that handles the frontmatter-aware splice correctly.
  The frontmatter-defaults-correctly invariant is the
  single most common bug in the manual recipe (an
  agent that prepends *above* the YAML block breaks
  every frontmatter consumer on the page); the new
  tool makes the default safe.
- **256 KiB body-size cap on every write tool** (T36) —
  uniform guardrail applied *before* the SB round trip
  on every write tool (`write_page`, `create_page`,
  `prepend_to_page`, `append_to_page`,
  `patch_page_lines`, `patch_page_replace`,
  `move_page`, `check_task`). Surfaces
  `ToolError("body too large: {size_bytes} bytes exceeds
  {cap} byte (256 KiB) cap; chunk into append_to_page
  calls")` with the size, the cap, and the remediation
  hint (matches `xmatthewx/silverbullet-mcp-server`'s
  error wording). The cap is on the caller-supplied
  body — ``len(body.encode("utf-8"))`` measurement,
  UTF-8 byte count (multi-byte chars count as their
  UTF-8 byte count, not codepoint count). 256 KiB
  exactly is the inclusive boundary (256 KiB passes,
  256 KiB + 1 byte fails). `move_page` is the one
  exception: the cap fires on the source body the
  bridge reads (because there's no caller-supplied
  body on `move_page` — the destination write carries
  the source body verbatim). `check_task` caps the
  post-shaping body (the page with the bullet
  flipped). Does NOT apply to read-side tools
  (`read_page`, `list_pages`, `page_exists`,
  `diff_pages`, `list_tasks`) or to the
  journal-discovery tools (`pages_touching_topic` /
  `search_pages` / `find_backlinks`).

  The cap composes cleanly with T31b's post-write
  verification helper: the cap fires *before* the
  PUT, so a too-large body never reaches the T31b
  re-read path (which runs only after a successful
  write). On the dry-run path, the cap fires before
  the read — no wasted FS walk on a doomed dry-run.

  Migration: no caller action needed. The cap is a
  uniform guardrail on every write tool; an agent
  that accidentally writes 600 KB to a journal page
  sees the clear `body too large` `ToolError` with
  the remediation hint, rather than a deferred
  failure at SB. The agent that wants to write a
  large page chunks via `append_to_page` (each chunk
  is capped at 256 KiB independently).
  that handles the frontmatter-aware splice correctly.
  The frontmatter-defaults-correctly invariant is the
  single most common bug in the manual recipe (an
  agent that prepends *above* the YAML block breaks
  every frontmatter consumer on the page); the new
  tool makes the default safe.

### Planned

- **T31 verification** (resolved 2026-08-30, **negative**):
  `tests/test_e2e_live_sb.py::test_if_match_stale_etag_returns_412`
  exercises a write → read × 2 → write with first-read etag →
  mutate out-of-band → write again with now-stale etag →
  assert 412, against the live SB. Result: SB silently
  overwrote the page on the stale-etag write (`is_error=False`,
  `size_bytes=53`). SB on this dev box returns no `ETag` on
  PUT responses either, so the bridge has no real etag to
  thread — the test falls back to a synthetic etag built
  from `X-Last-Modified` + `X-Content-Length` to verify the
  bridge's `If-Match` plumbing is wired correctly (it is).
  Two new follow-up tickets charted:
  - **T31a** — synthesize a fallback etag from
    `X-Last-Modified` + `X-Content-Length` when SB strips
    `ETag` (a `synthesize_etag(last_modified_ms, size_bytes)`
    helper in `sb_client.py`; stable across reads of the same
    body, drifts on different bodies).
  - **T31b** — replace the `If-Match`-only path with a
    post-write verification step: re-read after the PUT and
    compare the new etag against the `if_match` the caller
    passed; raise `ToolError("concurrent edit detected: …")`
    on mismatch. The existing 412 path still wins on SBs
    that honor `If-Match` (cheaper); the helper is the
    fallback for SBs that don't (which is this dev box).
    Threaded into `write_page`, `append_to_page`,
    `patch_page_lines`, `patch_page_replace`, `move_page`,
    `check_task`.

## [v1.2] — agent-facing QOL + bullet primitives

Build map: [`docs/wayfinder/map-v1.2.md`](docs/wayfinder/map-v1.2.md).
**Status: destination reached** — every ticket on the v1.2 map
(T23–T30) is closed; the bridge now exposes twelve tools.

### Added

- **`check_task(page, ref, state="done", if_match?, dry_run=False)`**
  — flip a checkbox bullet's state by its wikilink ref (T30).
  Reads the page, finds the unique bullet whose wikilink target
  equals `ref` (case-sensitive, matching `list_tasks` and SB's
  case-sensitive page lookup), flips the marker character, and
  writes the body back via `PUT /.fs/{page}` with `If-Match:
  <read_etag>` so a concurrent edit fails 412 rather than
  silently clobbering the flip. `state="done"` (default) flips
  to `[x]`, `state="todo"` flips to `[ ]`, `state="cancelled"`
  flips to `[X]` (SB's third state); any other value is
  `ToolError("state must be one of: done, todo, cancelled")`
  upfront, no read round trip. The rest of the line (leading
  whitespace, the dash, the bullet text, the wikilink itself)
  is preserved verbatim — only the character inside the square
  brackets changes. Returns the T23 ack envelope
  (`{name, etag, size_bytes, last_modified_ms, created_ms}`).

  Errors:
  - empty `ref` upfront → `ToolError("ref must not be empty")`;
  - missing page → standard `ToolError("page not found: {page}")`;
  - no bullet with the ref → `ToolError("no task with ref {ref} on page {page}; the task may not have a wikilink ref or may live on a different page")`;
  - multiple bullets with the ref → `ToolError("ref {ref} matches multiple tasks on page {page}; narrow the ref or use patch_page_lines directly")`;
  - stale etag → standard 412 ToolError.

  `dry_run=True` returns the T26 preview envelope
  (`{dry_run, original, patched, diff}`) without writing — the
  read still happens, the in-memory flip is computed,
  `if_match=<etag>` is checked against the read's etag, and the
  pre-read input-validation errors (empty `ref`, unknown
  `state`) still fire on dry-run.

### Changed

- **T23 (BREAKING): every write tool's return type widened from
  `str | None` (the new ETag) to a dict acknowledgement envelope.**
  Affected tools: `write_page`, `delete_page`, `append_to_page`,
  `patch_page_lines`, `patch_page_replace`, `move_page`. The new
  shape:

  ```jsonc
  {
    "name": "<page>",                          // string; same as the page you wrote
    "etag": "\"abc123\"",                       // string with quotes; null if SB stripped it
    "size_bytes": 1024,                         // UTF-8 byte count of the just-written body
    "last_modified_ms": 1700000000123,          // epoch ms; null if SB stripped it
    "created_ms": 1700000000000                 // epoch ms; null if SB stripped it
  }
  ```

  Migration: replace `etag = result.text` with
  `payload = result["result"]; etag = payload["etag"]` (or read
  `payload["size_bytes"]` / `payload["last_modified_ms"]` /
  `payload["created_ms"]` to skip the follow-up read v1.1 had to do
  to learn the same facts). See
  [README § v1.2 wire-shape changes](README.md#v12-wire-shape-changes)
  for the full migration note.

  `size_bytes` is always populated from the body the bridge just
  wrote (UTF-8 bytes — independent of whether SB echoed
  `X-Content-Length` back). `last_modified_ms` / `created_ms` /
  `etag` fall back to `null` on a fully-stripped response (older SB /
  proxy), same shape as the prior `None` ETag handling.

  `delete_page`'s `size_bytes` and both timestamps are `null`
  because SB's DELETE response doesn't echo `X-*` headers per the
  design doc § SilverBullet client contract DELETE row. An agent
  that wants the timestamps of what it's about to delete reads the
  page first and threads the etag into `if_match`.

  `move_page` returns the **destination's** envelope on success.
  The same-name no-op (`name == new_name`) returns the source's
  envelope (the read on the existence check now surfaces full meta
  since the client side was widened in the same change).

- **T24 (BREAKING): `read_page` and the `silverbullet://page/{name}`
  resource template's return type widened from `str` (raw markdown
  body) to a dict acknowledgement envelope.** The new shape:

  ```jsonc
  {
    "body": "<markdown>",                  // string; "" for an empty page
    "etag": "\"abc123\"",                   // string with quotes; null if SB stripped it
    "size_bytes": 1024,                     // UTF-8 byte count; null if SB stripped X-Content-Length
    "last_modified_ms": 1700000000123       // epoch ms; null if SB stripped X-Last-Modified
  }
  ```

  `name` and `created_ms` are deliberately dropped (the caller
  already passed `name` to the tool; reads have no create-vs-update
  distinction to surface, so `created_ms` would be noise). The
  underlying client already returned a `PageMeta` after T23; T24
  is a wire-shape widening of the read tool plus the matching
  resource template.

  Migration for the **tool**:
  `body = result.text` → `payload = result["result"]; body = payload["body"]`
  (or read `payload["etag"]` to skip the follow-up read that v1.1
  callers did to learn the etag before an `if_match` round-trip).

  Migration for the **resource template**:
  `context.text` was a raw markdown string; it is now a
  JSON-serialized envelope. Callers parse with
  `json.loads(context.text)["body"]`. The MIME type also flipped
  from `text/markdown` to `application/json` to match the
  structured envelope (the body's markdown is *inside* the JSON,
  not the wire content). See
  [README § v1.2 wire-shape changes](README.md#v12-wire-shape-changes)
  for the full migration note.

- **T28 (BREAKING): `list_pages`'s return type widened from
  `[{name, etag}]` (v1.1 minimal subset) to the same envelope
  family the read and write tools use.** Each row is now:

  ```jsonc
  {
    "name": "<page>",
    "etag": "\"abc123\"",        // null on this SB build (list payload omits it)
    "size_bytes": 1024,           // UTF-8 byte count; null if missing/malformed
    "last_modified_ms": 1700000000123,  // epoch ms; null if missing/malformed
    "created_ms": 1700000000000         // epoch ms; null if missing/malformed
  }
  ```

  Migration: replace
  `for row in result["result"]: name = row["name"]; etag = row["etag"]`
  with the same loop reading the same `name` / `etag` fields
  (their positions haven't moved) or read `size_bytes` /
  `last_modified_ms` / `created_ms` to skip the per-page
  `read_page` v1.1 callers did to learn those facts. See
  [README § v1.2 wire-shape changes](README.md#v12-wire-shape-changes).

  **Optional per-page etag-hydration** (T28's opt-in):
  `MCP_SILVERBULLET_LIST_PAGES_HYDRATE_ETAGS=1` enables one GET
  per row whose list-payload etag is `null`, hydrating the etag
  from the response's `ETag` header. Default off (the v1.1
  behaviour). Partial failures (one page 404'ing / 412'ing /
  5xx'ing / timing out during hydration) leave that row's etag
  as `null` rather than failing the whole call — the operator
  can retry the list later or `read_page` a specific page for
  the etag. Hydration is sequential (one GET at a time, no
  fan-out) to bound the concurrent connection count against
  loopback SB.

### Added

- **`list_tasks(page?, prefix?)`** — enumerate checkbox bullets
  on a page (per-page form, always available via
  `GET /.fs/{page}`) or across the whole space (space-walk
  form, requires `MCP_SILVERBULLET_JOURNAL_TOOLS=1` plus
  `MCP_SILVERBULLET_SPACE_PATH`). Returns one entry per
  checkbox bullet: `{name, ref, line, state, text}`. `name`
  is the page the bullet lives on (path relative to the space
  root for the space-walk form); `ref` is the wikilink target
  on the same line (`[[Pages/Hobbies]]` →
  `"Pages/Hobbies"`; an aliased `[[target|alias]]` strips the
  alias so the ref is the wikilink *target*, not the display
  text) or `null` when the bullet has no wikilink (such
  bullets are not addressable by `check_task`; use
  `patch_page_lines` for those). `line` is the 1-indexed
  editor line number (frontmatter included, matching what an
  SB editor highlights). `state` is the literal checkbox
  character: `" "` for `[ ]` (todo), `"x"` for `[x]` (done),
  `"X"` for `[X]` (cancelled — SB's third state). `text` is
  the bullet content after the checkbox marker. Frontmatter-
  block bullets are skipped (they're YAML config keys, not
  tasks). The space-walk form requires `page=None` (omit
  `page`) and an optional `prefix` substring against
  filenames; without the journal gate, omitting `page`
  surfaces `ToolError("list_tasks without page argument
  requires the journal surface to be enabled")` so the agent
  knows to fall back to the per-page form.

  Migration: none — the tool is additive. Agents that want a
  bullet inventory of a single page compose
  `read_page → regex match on "^- \[[ xX]\] "` today; swap
  to `list_tasks(page=name)` for a structured envelope.

- **`diff_pages(name, other_name?, other_body?)`** — line-based
  unified diff between two pages or a page and a literal
  markdown string (T27). Pass exactly one of `other_name` (a
  page to diff against) or `other_body` (a literal string);
  passing neither or both is rejected upfront with
  `ToolError("pass exactly one of other_name or other_body")`
  so the read round trip isn't wasted on a confused input
  shape. The wire shape is:

  ```jsonc
  {
    "diff": "--- first\n+++ second\n@@ -1 +1 @@\n-beta\n+BETA\n",
    // unified diff from ``difflib.unified_diff``; "" when the two bodies are identical
    "name": {                  // read-side envelope for the first page (caller passed)
      "name": "first",        // name is included so the shape is parallel with "other"
      "body": "alpha\nbeta\ngamma\n",
      "etag": "\"abc123\"",   // string with quotes; null if SB stripped it
      "size_bytes": 17,       // UTF-8 byte count; null if SB stripped X-Content-Length
      "last_modified_ms": 1700000000123  // epoch ms; null if SB stripped X-Last-Modified
    },
    "other": {                 // same envelope for the second page when other_name was given
      "name": "second",
      "body": "...",
      ...
    }                          // null when other_body (literal string) was given instead
  }
  ```

  Read-only — never writes. The diff is line-based by default
  (the v1.2 standing preference; token-level / word-level
  diffing is a v1.3 refinement). The 404 on either side
  surfaces as `ToolError("page not found: {name}")` with
  `name` set to whichever page was missing (the first read's
  404 short-circuits before the second; if the second read
  404s the wording's `name` field carries `other_name` so the
  agent can tell which side failed). 5xx / 412 / timeout on
  either read surface with the same wording as the read tool.

  Migration: none at the call-site — the tool is additive.

- **`page_exists(name)`** — cheap existence check (T25); issues
  `GET /.fs/{name}`, returns `bool`: `True` on 200, `False` on 404,
  `ToolError("silverbullet error: {status}")` on 5xx. The body
  bytes are never materialized, so the call is one round trip with
  the headers only — cheaper than `read_page` when the caller only
  wants a yes/no answer. A 5xx deliberately surfaces as an error
  rather than `False` so the caller can distinguish "no, proceed
  with create" from "SB is broken, don't make decisions".

  Migration: none — the tool is additive. If the caller has been
  composing `read_page → catch 404` to answer the same question,
  swap to `page_exists` for a body-free round trip.

- **`dry_run=True` knob on the three read-modify-write tools
  (T26).** `append_to_page(name, text, if_match?, dry_run=False)`,
  `patch_page_lines(name, start_line, end_line, new_content,
  if_match?, dry_run=False)`, and `patch_page_replace(name, find,
  new_string, replace_all=False, if_match?, dry_run=False)` now
  accept `dry_run=True` to preview a patch without committing. The
  read still happens (the tool needs the body to compute the
  patched version), the in-memory patch is computed the same way
  the live path computes it (same separator rule, same trailing-
  newline preservation, same `replace_all` semantics), and the
  tool returns a different envelope from the T23 write ack:

  ```jsonc
  {
    "dry_run": true,                 // always true on the dry-run path
    "original": "<markdown>",        // the body the tool read
    "patched": "<markdown>",         // the body that would have been written
    "diff": "--- original\n+++ patched\n@@ -1 +1 @@\n-hello\n+world\n"
    // unified diff from ``difflib.unified_diff``; "" for a no-op patch
  }
  ```

  `if_match=<etag>` is checked against the read's etag on the
  dry-run path because no PUT happens to do it on the server; a
  stale etag raises the same 412-equivalent `ToolError("precondition
  failed; check if_match/if_none_match")` as the live path so the
  agent sees one shape across both paths. `if_match="*"` (require
  existence) is enforced the same way the live read does — a
  missing page 404s on the read itself, before any etag check. All
  the pre-read input-validation errors still fire on dry-run (`text
  must not be empty`, `find must not be empty`, inverted range,
  etc.) — a caller with a bad input gets the same specific
  `ToolError` the live path would surface.

  Migration: none at the call-site for the live path (default
  `dry_run=False` preserves existing behavior). To use the new
  mode, pass `dry_run=True` and read the four-field envelope
  instead of the T23 write-ack shape. No PUT is ever issued on the
  dry-run path — verified by the Layer-1 test mock that asserts
  on the request methods the bridge issues.

## [v1.1] — full CRUD + editing

Build map: [`docs/wayfinder/map-v1.1.md`](docs/wayfinder/map-v1.1.md).

### Added

- **`delete_page(name, if_match?)`** — hard delete; returns the
  deleted page's ETag from `DELETE /.fs/{name}`.
- **`append_to_page(name, text, if_match?)`** — read-modify-write
  append; one newline separator inserted unless the body already
  ends in one; returns the new ETag.
- **`patch_page_lines(name, start_line, end_line, new_content, if_match?)`**
  — replace lines `start_line..end_line` (1-indexed, inclusive)
  with `new_content`; pass `new_content=""` to delete a range;
  preserves the page's trailing newline if it had one; returns the
  new ETag.
- **`patch_page_replace(name, find, new_string, replace_all=False, if_match?)`**
  — literal substring replace (no regex); `replace_all=False`
  errors when `find` matches more than once; returns the new ETag.
- **`move_page(name, new_name, if_match?)`** — write-then-delete
  rename with `If-None-Match: *` on the destination (never silently
  overwrites); atomicity-caveat wording on the source-delete step;
  same-name no-op; returns the new page's ETag.

### Changed

- **Bridge grew from three to eight `/.fs`-backed tools.**
- Every write tool honors `if_match` and returns the new ETag.

## [v1.0] — minimal runnable bridge

Build map: [`docs/wayfinder/map.md`](docs/wayfinder/map.md).

### Added

- **`read_page(name)`** — markdown body.
- **`write_page(name, content, if_match?)`** — create/update;
  returns the new ETag.
- **`list_pages(prefix?)`** — names + etags via `GET /.fs` with
  `X-Sync-Mode: 1`.
- **`silverbullet://page/{name}`** — resource template that wraps
  `read_page` for conversation-context attachment.
- Optional journal surface (gated by `MCP_SILVERBULLET_JOURNAL_TOOLS`
  + `MCP_SILVERBULLET_SPACE_PATH`):
  `journal_histogram`, `tag_summary`, `recent_pages`,
  `pages_touching_topic`.

[Unreleased]: #unreleased--v15-agent-experience-hardening
[v1.1]: #v11--full-crud--editing
[v1.0]: #v10--minimal-runnable-bridge

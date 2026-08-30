# Changelog

All notable changes to `mcp-silverbullet` are recorded here. The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
the project adheres to [Semantic Versioning](https://semver.org/).

Versions correspond to the build-map (wayfinder) charts under
`docs/wayfinder/`. The map for an in-flight version lists the open
tickets; this file records what's already shipped.

## [Unreleased] — v1.3 (agent-grade discovery + edit hygiene)

Build map: [`docs/wayfinder/map-v1.3.md`](docs/wayfinder/map-v1.3.md).
**Status: T31a / T31b / T32 / T33 / T34 / T35 / T36 shipped
(2026-08-30); v1.3 destination reached** — T31 closed
**negatively** on
2026-08-30, prompting the T31a + T31b follow-ups that are
now landed. T33 / T36 are unblocked.

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

[Unreleased]: #unreleased--v12-agent-facing-qol--bullet-primitives
[v1.1]: #v11--full-crud--editing
[v1.0]: #v10--minimal-runnable-bridge

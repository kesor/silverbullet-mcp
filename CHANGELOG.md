# Changelog

All notable changes to `mcp-silverbullet` are recorded here. The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
the project adheres to [Semantic Versioning](https://semver.org/).

Versions correspond to the build-map (wayfinder) charts under
`docs/wayfinder/`. The map for an in-flight version lists the open
tickets; this file records what's already shipped.

## [Unreleased] — v1.3 (agent-grade discovery + edit hygiene)

Build map: [`docs/wayfinder/map-v1.3.md`](docs/wayfinder/map-v1.3.md).
**Status: lead-blocked on T31** — six tickets charted; T31
(verify SB honors `If-Match` on `PUT /.fs/{name}`) is the lead
ticket and unblocks T32–T36 (everything else assumes the v1.2
concurrency story holds). T34 (`search_pages`) and T35
(`find_backlinks`) ship the journal surface into agent-facing
discovery; T32 (`create_page`), T33 (`prepend_to_page`), and T36
(256 KiB body-size cap) close the most common agent-side
friction points. See [`docs/competitive-landscape.md`](competitive-landscape.md)
for the research that fed this map.

### Planned

- **`create_page(name, content, if_match?)`** (T32) — refuse to
  overwrite as a first-class operation, distinct from
  `write_page`'s overwrite-or-create default. Thin wrapper over
  `write_page(if_match="*")`; translates the 412 path into a
  clean `ToolError("page already exists: {name}; use write_page
  to overwrite")`. Returns the T23 ack envelope on success.
- **`prepend_to_page(name, content, position="after_frontmatter"|"top",
  if_match?, dry_run=False)`** (T33) — mirrors `append_to_page`'s
  read-modify-write + `dry_run` shape but inserts at the top.
  Default `position="after_frontmatter"` (the human-meaningful
  case for journal / daily-notes pages with YAML frontmatter);
  `position="top"` overrides for the rare absolute-top intent.
- **`search_pages(query, prefix?, limit?)`** (T34) — substring
  content search. Thin wrapper over the v1
  `pages_touching_topic` journal machinery; returns the same
  `{name, snippet, match}` shape. Gated behind the journal
  surface (`MCP_SILVERBULLET_JOURNAL_TOOLS=1` +
  `MCP_SILVERBULLET_SPACE_PATH`).
- **`find_backlinks(target) -> [{file, line, text}]`** (T35) —
  wikilink-target backlinks. Walks the SB space directory,
  scans every `*.md` for `[[target]]` / `[[target|alias]]`
  references; returns one entry per match. Journal-gated.
- **256 KiB body-size cap on every write tool** (T36) — local
  cap applied before the PUT; surfaces
  `ToolError("body too large: {size_bytes} bytes exceeds 256
  KiB cap; chunk into append_to_page calls")` with the
  remediation hint naming the right next tool. Does NOT apply
  to read-side tools (`read_page`, `list_pages`, `page_exists`,
  `diff_pages`, `list_tasks`) or the journal-discovery tools.
- **T31 verification ticket** — single live-SB pytest case
  (`tests/test_e2e_live_sb.py`) that creates a page, reads it
  twice, issues a write with the first read's etag, then a
  second write with the first read's etag (now stale), and
  asserts the second call returns 412-equivalent `ToolError`.
  If the test passes, T31 closes positively and v1.2's
  `If-Match` assumption is verified; if it fails, T31a / T31b
  spawn to switch to `xmatthewx`-style `expected_last_modified`
  body-field convention (see `docs/competitive-landscape.md`
  § Code notes for the cutover reference).

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

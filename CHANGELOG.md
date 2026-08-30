# Changelog

All notable changes to `mcp-silverbullet` are recorded here. The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
the project adheres to [Semantic Versioning](https://semver.org/).

Versions correspond to the build-map (wayfinder) charts under
`docs/wayfinder/`. The map for an in-flight version lists the open
tickets; this file records what's already shipped.

## [Unreleased] — v1.2 (agent-facing QOL + bullet primitives)

Build map: [`docs/wayfinder/map-v1.2.md`](docs/wayfinder/map-v1.2.md).

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

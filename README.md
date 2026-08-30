# mcp-silverbullet

Model Context Protocol bridge between [SilverBullet](https://silverbullet.md)
and MCP clients (Grok Custom Connectors, `mcp` CLI, …). The bridge is a
side-car on loopback; it does not provision tunnels.

Architecture and threat model: [`docs/design.md`](docs/design.md).
Build map (v1, destination reached): [`docs/wayfinder/map.md`](docs/wayfinder/map.md).
v1.1 map (full CRUD + editing, destination reached with `move_page`): [`docs/wayfinder/map-v1.1.md`](docs/wayfinder/map-v1.1.md).
v1.2 map (agent-facing QOL + bullet primitives, two open tickets after T23+T24+T25+T26+T27+T28): [`docs/wayfinder/map-v1.2.md`](docs/wayfinder/map-v1.2.md).

## What it exposes

Ten tools and one resource template:

- `read_page(name)` — markdown body and metadata; returns `{body, etag, size_bytes, last_modified_ms}` (T24 ack envelope, see [§ v1.2 wire-shape changes](#v12-wire-shape-changes))
- `page_exists(name)` — cheap existence check; returns `bool` (T25). `True` on 200, `False` on 404, `ToolError` on 5xx so "no, proceed" stays distinct from "SB is broken". Doesn't materialize the body.
- `write_page(name, content, if_match?)` — create/update; returns `{name, etag, size_bytes, last_modified_ms, created_ms}` (T23 acknowledgement envelope)
- `append_to_page(name, text, if_match?, dry_run=False)` — read-modify-write append (one newline separator inserted unless the body already ends in one); returns the T23 ack envelope. With `dry_run=True` (T26) returns `{dry_run, original, patched, diff}` without writing — the read still happens and `if_match=<etag>` is checked against the read's etag so a stale etag raises 412-equivalent `ToolError` (the agent sees the same wording as the live path).
- `patch_page_lines(name, start_line, end_line, new_content, if_match?, dry_run=False)` — replace lines `start_line..end_line` (1-indexed, inclusive) with `new_content`; pass `new_content=""` to delete a range; preserves the page's trailing newline if it had one; returns the T23 ack envelope. `dry_run=True` (T26) returns the same `{dry_run, original, patched, diff}` preview as `append_to_page`.
- `patch_page_replace(name, find, new_string, replace_all=False, if_match?, dry_run=False)` — literal substring replace (no regex); `replace_all=False` (the safe default) errors if `find` matches more than once, so a typo never silently mass-edits; returns the T23 ack envelope. `dry_run=True` (T26) returns the same `{dry_run, original, patched, diff}` preview as the others.
- `move_page(name, new_name, if_match?)` — rename a page (write-then-delete so a partial failure leaves the body at the new name); destination always refuses to overwrite (`If-None-Match: *`); returns the destination's T23 ack envelope (the same-name no-op returns the source's)
- `delete_page(name, if_match?)` — hard delete; returns `{name, etag, size_bytes=None, last_modified_ms=None, created_ms=None}` (DELETE doesn't echo timestamps / size per SB's contract)
- `list_pages(prefix?)` — returns `[{name, etag, size_bytes, last_modified_ms, created_ms}][]` (T28 widened the per-row shape from the v1.1 `[{name, etag}]` to the same envelope family the read/write tools use; sends `X-Sync-Mode: 1` so SB 2.x returns JSON from `GET /.fs` instead of 307-redirecting to the SPA). On this SB build the list payload omits the `etag` field, so etags are `null` unless the operator opts in to per-page hydration via `MCP_SILVERBULLET_LIST_PAGES_HYDRATE_ETAGS=1` (one GET per row; partial failures leave the affected row's etag as `null` rather than failing the whole call).
- `diff_pages(name, other_name?, other_body?)` — line-based unified diff between two pages (or a page and a literal string); returns `{diff, name, other?}`. The wire shape: `diff` is a `difflib.unified_diff` between the two bodies (empty string for a no-op diff), `name` is the read-side envelope for the first page (`{name, body, etag, size_bytes, last_modified_ms}`), and `other` is the same envelope for the second page (only present when `other_name` was given; `None` when `other_body` was given). Read-only — never writes. Pass exactly one of `other_name` / `other_body`; passing neither or both is `ToolError("pass exactly one of other_name or other_body")` upfront, no wasted read.
- `silverbullet://page/{name}` — JSON envelope `{body, etag, size_bytes, last_modified_ms}` (same shape as `read_page`); MIME type is `application/json` in T24 (was `text/markdown` in v1.1)

Every write tool honors `if_match` (`"*"` to require existence,
`<etag>` to require an exact body match, `None` for unconditional).
Concurrent edits from two clients fail with a 412 — the second
client's stale etag does not overwrite the first client's write.

### v1.2 wire-shape changes

v1.2 is a **breaking change** for any client pinned to the v1.1 wire
shapes. Every read/write tool's return value widens from a bare
string (or `None` when SB stripped the header) to an envelope
dict. The write-tool shape (T23) and the read-tool shape (T24)
share the same meta fields, with two differences: writes carry
`name` and `created_ms` (the caller's identity and the page's
birth time), reads carry `body` and drop `name` (caller already
passed it) and `created_ms` (reads have no create-vs-update
distinction to surface).

Write-tool envelope (T23):

```jsonc
{
  "name": "<page>",
  "etag": "\"abc123\"",        // string with the surrounding quotes; null if stripped
  "size_bytes": 1024,           // UTF-8 byte count of the just-written body
  "last_modified_ms": 1700000000123,  // epoch ms; null if stripped
  "created_ms": 1700000000000         // epoch ms; null if stripped
}
```

Read-tool envelope (T24), used by `read_page` and the
`silverbullet://page/{name}` resource template:

```jsonc
{
  "body": "<markdown>",        // the page body; empty string for a blank page
  "etag": "\"abc123\"",        // string with the surrounding quotes; null if stripped
  "size_bytes": 1024,           // UTF-8 byte count; null if SB stripped X-Content-Length
  "last_modified_ms": 1700000000123  // epoch ms; null if SB stripped X-Last-Modified
}
```

The migration for v1.1 callers is one line per surface:

- **Write tools** (T23): replace `etag = result.text` with
  `etag = result["result"]["etag"]` (or read
  `result["result"]["size_bytes"]` / `last_modified_ms` /
  `created_ms` to skip the follow-up read v1.1 had to do to learn
  the same facts).
- **`read_page`** (T24): replace `body = result.text` with
  `body = result["result"]["body"]` (or read
  `result["result"]["etag"]` / `size_bytes` / `last_modified_ms`
  to skip the follow-up `read_page` that v1.1 had to do to learn
  the etag before a conditional write).
- **`silverbullet://page/{name}` resource** (T24): replace
  `context.text` with `json.loads(context.text)["body"]`, and
  update MIME-type expectations from `text/markdown` to
  `application/json` (the value is now a structured envelope).

`size_bytes` is always populated from the body the bridge just
wrote (independent of whether SB echoed `X-Content-Length` back).
The `last_modified_ms` / `created_ms` / `etag` fields fall back
to `null` on a fully-stripped response (older SB / proxy), same
shape as the prior `None` ETag handling. DELETE surfaces
`size_bytes=None` and both timestamps as `None` because SB's
DELETE response doesn't echo the `X-*` headers per the design doc
§ SilverBullet client contract.

The seven write tools (`write_page`, `delete_page`,
`append_to_page`, `patch_page_lines`, `patch_page_replace`,
`move_page`), the two read surfaces (`read_page`,
`silverbullet://page/{name}`), and `list_pages` (T28) all
return this same envelope family. `list_pages` returns one
envelope per row (no `body` field per row — use `read_page`
for the body).

Inbound MCP and outbound SilverBullet share one bearer secret by default.

### Dry-run mode (T26)

The three read-modify-write tools — `append_to_page`,
`patch_page_lines`, `patch_page_replace` — accept a
`dry_run: bool = False` knob. When `dry_run=True`:

- The tool still reads the page (it needs the body to compute the
  patched version).
- The in-memory patch is computed the same way the live path
  computes it (same separator rule for `append_to_page`, same
  trailing-newline preservation for `patch_page_lines`, same
  `replace_all` semantics for `patch_page_replace`).
- No PUT is issued — the page is left untouched.
- `if_match=<etag>` is checked against the **read's** etag. A
  stale etag raises 412-equivalent `ToolError("precondition failed;
  check if_match/if_none_match")` so the agent sees the same
  shape as the live path, not a vague "would have failed".
- `if_match="*"` (require existence) is enforced the same way the
  live read does — a missing page 404s on the read itself, before
  any etag check.
- All the pre-read input-validation errors still fire on dry-run
  (`text must not be empty`, `find must not be empty`, inverted
  range, etc.) — a caller with a bad `find` gets the same specific
  `ToolError` the live path would surface, not a vague preview.

The return shape on `dry_run=True` is a different envelope from
T23's write ack:

```jsonc
{
  "dry_run": true,                 // always true on the dry-run path
  "original": "<markdown>",        // the body the tool read
  "patched": "<markdown>",         // the body that would have been written
  "diff": "--- original\n+++ patched\n@@ -1 +1 @@\n-hello\n+world\n"
  // unified diff from ``difflib.unified_diff``; ``""`` for a no-op patch
}
```

`original` and `patched` are the markdown bodies (UTF-8 strings,
possibly empty). `diff` is a unified diff between them, suitable
for showing a human what the patch would change. The agent can
choose to apply the patch by calling the tool again with
`dry_run=False`, or surface the diff to the user for confirmation.

## Optional: journal surface (T10–T12)

If the bridge process also has read access to the SB space directory
(typical on the same machine that runs SilverBullet; rare behind a
containerized split), four direct-FS read tools can be enabled:

- `journal_histogram(prefix?)` — bucket `*.md` pages by `YYYY-MM`
- `tag_summary(prefix?)` — count occurrences of every `tags:` value
- `recent_pages(limit?, prefix?)` — newest pages by mtime
- `pages_touching_topic(query, prefix?)` — case-insensitive name+content substring search; returns `{name, match, snippet}[]` where `match` is `"name"`, `"content"`, or `"both"` and `snippet` is an ~80-char window centered on the content match (or a body excerpt for name-only matches). Uses `rg --json` when `rg` is on `PATH`; falls back to a pure-Python substring scan otherwise.

They are strictly additive: the `/.fs`-backed tools above continue to
work whether the journal surface is on or off. Set both
`MCP_SILVERBULLET_SPACE_PATH` (absolute path to the space) and
`MCP_SILVERBULLET_JOURNAL_TOOLS=1` (any truthy value) to opt in.
Without one of them, the bridge boots cleanly without the journal
tools and logs a single `INFO`/`WARN` line so the operator can see
which branch ran.

## Requirements

- Nix (flake) **or** Python 3.11–3.13 + [uv](https://docs.astral.sh/uv/)
- A running SilverBullet (`/.fs` HTTP API)
- Optional: an existing Cloudflare tunnel (or nginx) in front of `127.0.0.1:8000`

## Boot order

1. **Generate a token** (any high-entropy string). This is `T` below.

   ```bash
   T=$(openssl rand -hex 32)
   ```

2. **SilverBullet** on loopback, same secret if SB auth is on:

   ```bash
   # example; use whatever already runs your space
   SB_AUTH_TOKEN=$T silverbullet --hostname 127.0.0.1 --port 3000 /path/to/space
   ```

   If this SilverBullet has **no** auth (dev box), leave SB without a token
   and set `MCP_SILVERBULLET_SB_TOKEN` empty on the bridge (step 3).

3. **Bridge** — from a checkout:

   ```bash
   export MCP_SILVERBULLET_TOKEN=$T
   export MCP_SILVERBULLET_SB_URL=http://127.0.0.1:3000
   # optional: empty when SB has no auth
   # export MCP_SILVERBULLET_SB_TOKEN=
   # optional: public URL stamped into WWW-Authenticate + discovery
   # export MCP_SILVERBULLET_RESOURCE_URL=https://<tunnel>/mcp
   # optional: extra Host values when nginx/cloudflared forward a public name
   # export MCP_SILVERBULLET_ALLOWED_HOSTS=<mcp>.local,<tunnel>.trycloudflare.com
   nix run .#mcp-silverbullet
   ```

   Equivalent without Nix: `uv sync && uv run mcp-silverbullet`.
   Listens on `http://127.0.0.1:8000/mcp` by default
   (`MCP_SILVERBULLET_HOST` / `MCP_SILVERBULLET_PORT`).

4. **Tunnel** (operator-owned; this repo does not start `cloudflared`):

   ```bash
   cloudflared tunnel --url http://127.0.0.1:8000
   ```

5. **Client** — paste `https://<tunnel>/mcp` and bearer `T` into a Grok
   Custom Connector, or:

   ```bash
   MCP_SILVERBULLET_TOKEN=$T mcp dev http://127.0.0.1:8000/mcp
   ```

If a quick tunnel URL rotates, the token stays; re-paste the new URL.

## Use from a Pi coding agent session

The repo ships with a project-local `.mcp.json` so a Pi session
running in this checkout discovers the bridge automatically (via the
`pi-mcp-adapter` extension). After `python -m mcp_silverbullet` (or
`nix run .#mcp-silverbullet`) is running on `127.0.0.1:8000`, run
`/reload` in Pi and the bridge's ten tools — `read_page`,
`page_exists`, `write_page`, `append_to_page`, `patch_page_lines`,
`patch_page_replace`, `move_page`, `delete_page`, `list_pages`,
`diff_pages` — register as direct Pi tools.

The bearer token is read at HTTP-connect time via the `!command`
syntax, pointed at `~/.config/mcp-silverbullet/token` (mode 600) so
the secret stays out of the repo and out of Pi's process env. Generate
it once:

```bash
python -c 'import secrets; print(secrets.token_hex(32))' \
  > ~/.config/mcp-silverbullet/token
chmod 600 ~/.config/mcp-silverbullet/token
```

Then start the bridge with that same token in its env:

```bash
export MCP_SILVERBULLET_TOKEN=$(cat ~/.config/mcp-silverbullet/token)
export MCP_SILVERBULLET_SB_URL=http://127.0.0.1:63000  # or wherever SB listens
export MCP_SILVERBULLET_SB_TOKEN=                      # empty if SB has no auth
nix run .#mcp-silverbullet
```

The bridge is a side-car, not a daemon: it has to be running for the
tools to work, and `lifecycle: lazy` in `.mcp.json` means Pi won't
try to connect until the first tool call.

## Env vars

| Variable | Default | Role |
|---|---|---|
| `MCP_SILVERBULLET_TOKEN` | *(required)* | Inbound `Authorization: Bearer` |
| `MCP_SILVERBULLET_SB_URL` | `http://127.0.0.1:3000` | SilverBullet origin |
| `MCP_SILVERBULLET_SB_TOKEN` | same as `MCP_SILVERBULLET_TOKEN` | Outbound SB bearer; empty string = no header |
| `MCP_SILVERBULLET_RESOURCE_URL` | `http://127.0.0.1:8000/mcp` | Discovery + `WWW-Authenticate` |
| `MCP_SILVERBULLET_HOST` | `127.0.0.1` | Bind address |
| `MCP_SILVERBULLET_PORT` | `8000` | Bind port |
| `MCP_SILVERBULLET_ALLOWED_HOSTS` | *(unset → SDK loopback default)* | Extra `Host` values, comma-separated |
| `MCP_SILVERBULLET_SPACE_PATH` | *(unset)* | Absolute path to the SB space directory; required to enable the journal surface |
| `MCP_SILVERBULLET_JOURNAL_TOOLS` | *(unset)* | Truthy (`1` / `true` / `yes` / `on`) enables the four journal tools above; requires `MCP_SILVERBULLET_SPACE_PATH` to be set and readable |
| `MCP_SILVERBULLET_LIST_PAGES_HYDRATE_ETAGS` | *(unset)* | Truthy enables per-page etag-hydration on `list_pages` (T28). Default off (N+1 cost is opt-in). The SB list payload omits the etag field on this build; an operator who needs `if_match` round-trips from a list call pays one GET per row to hydrate. Partial failures (404 / 412 / 5xx / timeout on one page) leave that row's etag as `null` rather than failing the whole call. |

Live pytest against a real space (T7): set `MCP_SILVERBULLET_LIVE_SB_URL`
(e.g. `http://127.0.0.1:63000`) and `MCP_SILVERBULLET_LIVE_SB_TOKEN`
(empty string is fine if SB has no auth). Unset → tests skip.

Live pytest against the journal surface (T13): set
`MCP_SILVERBULLET_LIVE_SPACE_PATH` to the absolute path of an SB
space directory (e.g. `/var/lib/silverbullet`). The journal test
exercises `journal_histogram` / `tag_summary` / `recent_pages`
against that directory and is independent of any running SB. Unset →
test skips.

## Dev

```bash
nix develop          # editable source + pytest
pytest               # Layer 1–2, no live SB
nix flake check
```

MCP SDK is pinned at `mcp==2.1.1` (`uv.lock`). License: MIT.

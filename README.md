# mcp-silverbullet

Model Context Protocol bridge between [SilverBullet](https://silverbullet.md)
and MCP clients (Grok Custom Connectors, `mcp` CLI, …). The bridge is a
side-car on loopback; it does not provision tunnels.

Architecture and threat model: [`docs/design.md`](docs/design.md).

## What it exposes

**Fourteen tools + one resource template.** Every write tool returns
the new etag on every successful write — read it and feed it back via
`if_match` on the next call to fail fast on stale etags.

- `read_page(name)` → `{body, etag, size_bytes, last_modified_ms}`
  — markdown body and metadata. `etag` is `None` if SB stripped the
  `ETag` response header.
- `page_exists(name)` → `bool` — `True` on 200, `False` on 404,
  `ToolError` on 5xx so "no, proceed" stays distinct from "SB is broken".
- `write_page(name, content, if_match?)` → `{name, etag, size_bytes,
  last_modified_ms, created_ms}` — create or overwrite.
- `create_page(name, content)` → same envelope as `write_page` — but
  refuses to overwrite an existing page, surfacing
  `ToolError("page already exists: {name}; use write_page to overwrite")`
  on collision. `if_match="*"` is implied; use `write_page` directly
  if you want to write with a precondition.
- `append_to_page(name, text, if_match?, dry_run=False)` — read-modify-
  write append (one newline separator inserted unless the body
  already ends in one); returns the same envelope. With
  `dry_run=True` returns `{dry_run, original, patched, diff}` without
  writing.
- `prepend_to_page(name, content, position="after_frontmatter"|"top",
  if_match?, dry_run=False)` — top-of-body insert with YAML
  frontmatter awareness. Default `position="after_frontmatter"`
  inserts the new content *between* the closing `---` of the frontmatter
  block and the first body line (the human-meaningful default for
  journal / daily-notes pages); `position="top"` overrides and inserts
  above the frontmatter. Both positions produce the same splice on
  pages without frontmatter. Malformed frontmatter (opening fence but
  no close) is treated as no-frontmatter.
- `patch_page_lines(name, start_line, end_line, new_content, if_match?,
  dry_run=False)` — replace lines `start_line..end_line` (1-indexed,
  inclusive) with `new_content`; pass `new_content=""` to delete a
  range; preserves the page's trailing newline if it had one.
- `patch_page_replace(name, find, new_string, replace_all=False,
  if_match?, dry_run=False)` — literal substring replace (no regex);
  `replace_all=False` (the safe default) errors if `find` matches
  more than once, so a typo never silently mass-edits.
- `move_page(name, new_name, if_match?)` — rename (write-then-delete
  so a partial failure leaves the body at the new name); destination
  always refuses to overwrite.
- `delete_page(name, if_match?)` → `{name, etag, size_bytes=None,
  last_modified_ms=None, created_ms=None}` — hard delete; SB's
  DELETE response doesn't echo timestamps / size.
- `list_pages(prefix?)` → `[{name, etag, size_bytes, last_modified_ms,
  created_ms}][]` — sends `X-Sync-Mode: 1` so SB 2.x returns JSON
  from `GET /.fs` instead of 307-redirecting to the SPA. The etag
  field is `null` on this SB build unless you opt in to per-page
  hydration with `MCP_SILVERBULLET_LIST_PAGES_HYDRATE_ETAGS=1`
  (one GET per row; partial failures leave the affected row's etag
  as `null` rather than failing the whole call).
- `diff_pages(name, other_name?, other_body?)` → `{diff, name, other?}`
  — line-based unified diff between two pages (or a page and a
  literal string). Pass exactly one of `other_name` / `other_body`;
  passing neither or both is `ToolError("pass exactly one of
  other_name or other_body")` upfront, no wasted read.
- `list_tasks(page?, prefix?)` → `[{name, ref, line, state, text}][]`
  — enumerate checkbox bullets on a page (per-page form, always
  available via `GET /.fs/{page}`) or across the whole space
  (space-walk form, requires `MCP_SILVERBULLET_JOURNAL_TOOLS=1` and
  `MCP_SILVERBULLET_SPACE_PATH`). `state` is the literal checkbox
  character (`" "` for `[ ]`, `"x"` for `[x]`, `"X"` for `[X]`).
  Frontmatter-block bullets are skipped.
- `check_task(page, ref, state="done", if_match?, dry_run=False)` —
  flip a checkbox bullet's state by its wikilink ref. Reads the page,
  finds the unique bullet whose wikilink target equals `ref`, flips
  the marker, writes the body back via `PUT /.fs/{page}` with
  `If-Match: <read_etag>` so a concurrent edit fails rather than
  silently clobbering. `state="done"` flips to `[x]`, `"todo"` to
  `[ ]`, `"cancelled"` to `[X]`.
- `silverbullet://page/{name}` → JSON envelope `{body, etag,
  size_bytes, last_modified_ms}` (same shape as `read_page`; MIME
  type `application/json`).

### Concurrency: read-then-write with current etag

Every write tool accepts `if_match` (`"*"` to require existence,
`<etag>` to require an exact body match, `None` for unconditional).
On this build, SilverBullet does **not** honor `If-Match` and does
**not** return `ETag` on PUT — the bridge synthesizes a fallback etag
from `X-Last-Modified` + `X-Content-Length` and runs a post-write
re-read to compare. If a stale etag slips through, the bridge raises
`ToolError("concurrent edit detected: …; read it again and re-issue
the write with the current etag")`. The fix is always the same: read
the page again, take the new `etag` from the response, retry the write.

**Every successful write returns the new `etag`.** Pass it to the next
`if_match` on the same page and you'll never see the concurrency error.

### Dry-run mode

`append_to_page` / `prepend_to_page` / `patch_page_lines` /
`patch_page_replace` / `check_task` accept `dry_run=True` to preview
a patch without committing. The read still happens (the tool needs
the body to compute the patch), `if_match` is validated against the
read's etag, and the response is `{dry_run, original, patched, diff}`.
A no-op patch returns an empty `diff`.

### Optional: journal surface

Six additional tools are enabled when the bridge has direct read
access to the SB space directory (typical on the same host that runs
SilverBullet; rare behind a containerized split). Set both
`MCP_SILVERBULLET_SPACE_PATH` (absolute path to the space) and
`MCP_SILVERBULLET_JOURNAL_TOOLS=1` to opt in. Without either, the
bridge boots cleanly without these tools and logs a single
INFO/WARN line.

- `journal_histogram(prefix?)` — bucket `*.md` pages by `YYYY-MM`.
- `tag_summary(prefix?)` — count occurrences of every `tags:` value.
- `recent_pages(limit?, prefix?)` — newest pages by mtime.
- `pages_touching_topic(query, prefix?)` — case-insensitive
  name+content substring search; returns `{name, match, snippet}[]`
  (`match` is `"name"`, `"content"`, or `"both"`). Uses `rg --json`
  when available, falls back to pure-Python otherwise.
- `search_pages(query, prefix?, limit=20)` — bounded variant of
  `pages_touching_topic` with a `limit` knob (default 20, hard cap
  100). Same wire shape. Use this when you want the top N hits;
  `pages_touching_topic` for unbounded scans.
- `find_backlinks(target) -> [{file, line, text}]` — wikilink-target
  backlinks for the rename-pre-flight workflow. `file` is the
  relative path to the linking page, `line` is the 1-indexed editor
  line number, `text` is the stripped line. Target normalization:
  leading/trailing slashes and a trailing `.md` are stripped; aliases
  (`[[target|alias]]`) match the bare target. Self-links are returned
  (filter client-side). Empty / whitespace-only `target` raises
  `ToolError("target must not be empty")` upfront.

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
   SB_AUTH_TOKEN=$T silverbullet --hostname 127.0.0.1 --port 3000 /path/to/space
   ```

   If your SilverBullet has **no** auth (dev box), leave SB without a
   token and set `MCP_SILVERBULLET_SB_TOKEN` empty on the bridge
   (step 3).

3. **Bridge** — from a checkout. The bridge defaults to JWT mode
   (validates tokens against an IdP's JWKS); set
   `MCP_SILVERBULLET_AUTH_MODE=static` for the legacy shared-secret
   surface (used by `mcp dev` and other non-IdP setups).

   ```bash
   # JWT mode (default — bridge sits behind Cloudflare Access,
   # Auth0, Okta, Google-IAP, …; validates per-user tokens against
   # the IdP's JWKS):
   export MCP_SILVERBULLET_JWT_ISSUER=https://<org>.cloudflareaccess.com
   export MCP_SILVERBULLET_JWT_AUDIENCE=<AUD-tag-from-CF-dashboard>
   export MCP_SILVERBULLET_JWT_JWKS_URL=https://<org>.cloudflareaccess.com/cdn-cgi/access/certs
   export MCP_SILVERBULLET_SB_URL=http://127.0.0.1:3000
   nix run .#mcp-silverbullet

   # Static mode (legacy shared-secret):
   export MCP_SILVERBULLET_AUTH_MODE=static
   export MCP_SILVERBULLET_TOKEN=$T
   export MCP_SILVERBULLET_SB_URL=http://127.0.0.1:3000
   nix run .#mcp-silverbullet
   ```

   Common knobs (both modes):

   ```bash
   # optional: empty when SB has no auth
   # export MCP_SILVERBULLET_SB_TOKEN=
   # optional: public URL stamped into WWW-Authenticate + discovery
   # export MCP_SILVERBULLET_RESOURCE_URL=https://<tunnel>/mcp
   # optional: extra Host values when nginx/cloudflared forward a public name
   # export MCP_SILVERBULLET_ALLOWED_HOSTS=<mcp>.local,<tunnel>.trycloudflare.com
   ```

   Equivalent without Nix: `uv sync && uv run mcp-silverbullet`.
   Listens on `http://127.0.0.1:8000/mcp` by default
   (`MCP_SILVERBULLET_HOST` / `MCP_SILVERBULLET_PORT`).

4. **Tunnel** (operator-owned; this repo does not start `cloudflared`):

   ```bash
   cloudflared tunnel --url http://127.0.0.1:8000
   ```

5. **Client** — in JWT mode, paste `https://<tunnel>/mcp` and let
   the IdP handle auth (the bridge trusts whatever IdP-issued JWT
   reaches it). In static mode, paste the bearer too:

   ```bash
   MCP_SILVERBULLET_AUTH_MODE=static MCP_SILVERBULLET_TOKEN=$T \
     mcp dev http://127.0.0.1:8000/mcp
   ```

   If a quick-tunnel URL rotates, the bearer stays; re-paste the new URL.

## Use from a Pi coding agent session

The repo ships with a project-local `.mcp.json` so a Pi session
running in this checkout discovers the bridge automatically (via the
`pi-mcp-adapter` extension). After `python -m mcp_silverbullet` (or
`nix run .#mcp-silverbullet`) is running on `127.0.0.1:8000`, run
`/reload` in Pi and the bridge's fourteen always-on tools register as
direct Pi tools. The journal-surface tools register additionally
when `MCP_SILVERBULLET_JOURNAL_TOOLS=1` and `MCP_SILVERBULLET_SPACE_PATH`
are both set.

In static mode, the bearer token is read at HTTP-connect time via
the `!command` syntax in `.mcp.json`, pointed at
`~/.config/mcp-silverbullet/token` (mode 600):

```bash
python -c 'import secrets; print(secrets.token_hex(32))' \
  > ~/.config/mcp-silverbullet/token
chmod 600 ~/.config/mcp-silverbullet/token
```

The bridge is a side-car, not a daemon: it has to be running for the
tools to work, and `lifecycle: lazy` in `.mcp.json` means Pi won't
try to connect until the first tool call.

## Env vars

| Variable | Default | Role |
|---|---|---|
| `MCP_SILVERBULLET_AUTH_MODE` | `jwt` | `jwt` (default) validates per-user tokens against the IdP's JWKS via `MCP_SILVERBULLET_JWT_*`. `static` accepts a single shared secret via `MCP_SILVERBULLET_TOKEN` (legacy surface). |
| `MCP_SILVERBULLET_TOKEN` | *(required when `AUTH_MODE=static`; ignored otherwise)* | Shared bearer secret. Compared constant-time against the inbound `Authorization: Bearer …` header. |
| `MCP_SILVERBULLET_JWT_ISSUER` | *(required when `AUTH_MODE=jwt`)* | Expected `iss` claim. Cloudflare Access: `https://<org>.cloudflareaccess.com`. |
| `MCP_SILVERBULLET_JWT_AUDIENCE` | *(required when `AUTH_MODE=jwt`)* | Expected `aud` claim. Cloudflare Access: the per-application AUD tag from the Zero Trust dashboard. |
| `MCP_SILVERBULLET_JWT_JWKS_URL` | *(required when `AUTH_MODE=jwt`)* | JWKS endpoint URL. Cloudflare Access: `https://<org>.cloudflareaccess.com/cdn-cgi/access/certs`. |
| `MCP_SILVERBULLET_JWT_ALGORITHMS` | `RS256` | Comma-separated JWA algorithm allow-list. Pinned to `RS256` by default so the bridge refuses the classic algorithm-confusion attack (HS256-signed with the public key as secret). Operators on Auth0/Okta/Google-IAP pass the IdP's algorithm. |
| `MCP_SILVERBULLET_JWT_LEEWAY_SECONDS` | `30` | Clock-skew tolerance for `exp` / `iat` / `nbf`. CF Access's recommended value. |
| `MCP_SILVERBULLET_SB_URL` | `http://127.0.0.1:3000` | SilverBullet origin. |
| `MCP_SILVERBULLET_SB_TOKEN` | same as `MCP_SILVERBULLET_TOKEN` | Outbound SB bearer; empty string = no header. |
| `MCP_SILVERBULLET_RESOURCE_URL` | `http://127.0.0.1:8000/mcp` | Discovery + `WWW-Authenticate`. |
| `MCP_SILVERBULLET_HOST` | `127.0.0.1` | Bind address. |
| `MCP_SILVERBULLET_PORT` | `8000` | Bind port. |
| `MCP_SILVERBULLET_ALLOWED_HOSTS` | *(unset → SDK loopback default)* | Extra `Host` values, comma-separated. |
| `MCP_SILVERBULLET_SPACE_PATH` | *(unset)* | Absolute path to the SB space directory; required to enable the journal surface. |
| `MCP_SILVERBULLET_JOURNAL_TOOLS` | *(unset)* | Truthy (`1` / `true` / `yes` / `on`) enables the six journal tools; requires `MCP_SILVERBULLET_SPACE_PATH` to be set and readable. |
| `MCP_SILVERBULLET_LIST_PAGES_HYDRATE_ETAGS` | *(unset)* | Truthy enables per-page etag-hydration on `list_pages`. Default off (N+1 cost is opt-in). The SB list payload omits the etag field on this build; an operator who needs `if_match` round-trips from a list call pays one GET per row to hydrate. |
| `MCP_SILVERBULLET_LOG_LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` / `CRITICAL`. `DEBUG` dumps sanitized HTTP request metadata and classifies uvicorn's opaque `Invalid HTTP request received` warnings (HTTP/2 preface vs TLS ClientHello vs junk). `UVICORN_LOG_LEVEL` is ignored — uvicorn is configured from this var. |
| `MCP_SILVERBULLET_DEBUG` | *(unset)* | Truthy (`1` / `true` / `yes` / `on`) is a one-knob alias for `LOG_LEVEL=debug`. Explicit `LOG_LEVEL` wins if both are set. |

`WARNING: Invalid HTTP request received.` from uvicorn means the first bytes on the socket were not HTTP/1. This uvicorn build does not log *what* they were, even at debug. Set `MCP_SILVERBULLET_LOG_LEVEL=debug` (or `MCP_SILVERBULLET_DEBUG=1`) and the bridge logs a classification + the first 200 bytes next to that warning. Typical causes behind Cloudflare Access / cloudflared: HTTP/2 to an HTTP/1 origin (`http2-preface`), HTTPS hitting the HTTP port (`tls-clienthello`), or a TCP health check with no HTTP (`empty` / `unknown`).

## Dev

```bash
nix develop          # editable source + pytest
pytest               # Layer 1–2, no live SB
nix flake check
```

MCP SDK is pinned at `mcp==2.1.1` (`uv.lock`). License: MIT.
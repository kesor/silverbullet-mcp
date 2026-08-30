# `mcp-silverbullet` — design

> **Status**: design. No code in this repo yet. The companion wayfinder
> map (`docs/wayfinder/map.md`) tracked every decision the doc encodes;
> each section below is anchored back to the ticket that resolved it.

## What this is

`mcp-silverbullet` is a small Python service that bridges **Grok on the
web** (a remote MCP client) to a **local SilverBullet server** (an HTTP
file API on `/.fs/…`). The bridge exposes ten MCP **tools** —
`read_page`, `page_exists`, `write_page`, `append_to_page`,
`patch_page_lines`, `patch_page_replace`, `move_page`, `delete_page`,
`list_pages` —
and one MCP **resource template** — `silverbullet://page/{name}` —
letting Grok read and write your SilverBullet pages as conversation
context or as tool calls.

The bridge is meant to be **run behind the user's existing Cloudflare
tunnel** alongside SilverBullet. Neither service is exposed directly;
only the bridge is on the tunnel, and only Grok has the token.

## Goals, non-goals

- **Goal**: Grok on the web can `read_page`, `write_page`,
  `append_to_page`, `patch_page_lines`, `patch_page_replace`,
  `move_page`, `delete_page`, `list_pages`, `page_exists`, and
  `diff_pages` against SilverBullet, behind the user's existing
  Cloudflare tunnel, with one bearer token.
- **Non-goals**: MCP Apps (UI resources), OAuth 2.1, search,
  multi-user, mutating silver bullet's source, hosting the bridge
  for other people.

## Architecture at a glance

```
                          ┌──────── Cloudflare tunnel (user-owned) ────────┐
                          │                                                  │
Grok (grok.com/connectors)│                                                  │
        │                  │                                                  │
        │ HTTPS + Bearer T │                                                  │
        ▼                  │                                                  │
   https://<tunnel>/mcp   │                                                  │
                          ▼                                                  │
                 ┌───────────────────────┐                                   │
                 │  mcp-silverbullet     │                                   │
                 │  127.0.0.1:<port>     │                                   │
                 │  (this project)       │                                   │
                 └───────────────────────┘                                   │
                          │                                                  │
                          │ HTTP + Bearer T                                  │
                          ▼                                                  │
                 ┌───────────────────────┐                                   │
                 │  SilverBullet         │                                   │
                 │  127.0.0.1:3000       │                                   │
                 │  SB_AUTH_TOKEN=T      │                                   │
                 └───────────────────────┘                                   │
```

The bridge forwards the **same** bearer token it just verified on the
inbound hop to SilverBullet on the outbound hop. There is exactly one
secret in play.

## § Transport

Locked at T1. The bridge serves a single **Streamable HTTP** endpoint
on **`POST /mcp`**, in the **2026-07-28** spec era, **stateless**
posture.

- **Standard binding.** Streamable HTTP is one of only two standard
  MCP bindings (stdio and Streamable HTTP). HTTP+SSE was deprecated on
  2025-03-26 and reclassified as Deprecated under the feature-lifecycle
  policy on 2026-07-28; it is not what any current client is built
  against and Grok's own tooling flags it as fragile over a free
  Cloudflare tunnel.
- **Stateless server.** There is no protocol-level session. Every
  request carries its protocol version and capabilities in the
  `MCP-Protocol-Version` header and the `Mcp-Method`/`Mcp-Name` headers
  plus the JSON-RPC body's `_meta`. Cross-call state, if ever needed,
  is passed as ordinary tool arguments.
- **Per-request responses may be SSE.** A tool that emits
  `notifications/progress` mid-call (we don't ship one in v1) opens
  an SSE stream scoped to that POST, and the response is
  `Content-Type: text/event-stream`. Otherwise the response is a
  single `application/json` body.
- **Web-standard posture.** The Python SDK exposes
  `mcp.streamable_http_app()` as a web-standard ASGI app (Starlette).
  We run it under uvicorn. The `mcp.run(..., transport="streamable-
  http")` shortcut is fine if we never need extra routes.

## § Auth

Locked at T2. **Static bearer** on both hops, **one shared secret**.

### Wire

```
Grok ──Authorization: Bearer <T>──▶  mcp-silverbullet ──Authorization: Bearer <T>──▶ SilverBullet
```

T is read once from the bridge's env (`MCP_SILVERBULLET_TOKEN`) and
the same value is configured into SilverBullet's env
(`SB_AUTH_TOKEN`). The bridge forwards T verbatim.

### Bridge → Grok (inbound)

The Python SDK's `mcp.server.auth.TokenVerifier` is the only
integration point. We ship a 10-line implementation that
constant-time-compares the token against the one in env:

```python
class StaticTokenVerifier:
    def __init__(self, token: str) -> None:
        self._t = token
    async def verify_token(self, token: str) -> AccessToken | None:
        if hmac.compare_digest(token.encode(), self._t.encode()):
            return AccessToken(
                token=token, client_id="grok",
                scopes=["notes:read", "notes:write"],
                subject="local",
            )
        return None
```

The SDK handles every header-related chore: parsing `Authorization: Bearer …`,
serving the `/.well-known/oauth-protected-resource/mcp` discovery document,
returning `401 Unauthorized` + `WWW-Authenticate: Bearer resource_metadata=…`
when the token is missing or wrong. None of that is our code.

### Bridge → SilverBullet (outbound)

SilverBullet already has bearer-token auth baked in. Configure with:

```
SB_AUTH_TOKEN=<T>      # the same secret as above
SB_USER=               # not set — login flow is off
```

The `SB_AUTH_TOKEN` is the only auth layer in front of `/.fs/…`.
`server/src/auth/jwt_authorizer.rs::JwtAuthorizer::authorize` checks
`Authorization: Bearer …` in constant time and accepts the request
iff the token matches. Otherwise `401`.

We forward the inbound token unchanged using `httpx.AsyncClient`'s
default headers:

```python
self._sb = httpx.AsyncClient(
    base_url=sb_url,
    headers={"Authorization": f"Bearer {token}"},
    timeout=httpx.Timeout(10.0, connect=3.0),
)
```

### Token storage and rotation

- Generate: `openssl rand -hex 32`. One secret.
- Distribute: set `MCP_SILVERBULLET_TOKEN` in the bridge's environment
  and `SB_AUTH_TOKEN` in SilverBullet's environment — same value, both
  processes.
- Rotate: change the env var on both sides, restart both. There is no
  grace period and no session store, so an old token stops working
  the instant the processes reload.
- Cache: none. Tunnel URL rotation does not interact with token rotation;
  the token lives in the bridge env and Grok's connector dialog
  independently.

### What we are not doing

- **OAuth 2.1.** The Python SDK is built around it. For one user, one
  tunnel, one process, the threat model doesn't need an authorization
  server, refresh tokens, or dynamic-client registration. (Grok's
  custom-connector dialog accepts a static bearer natively.)
- **Multi-user.** Single user. Single token. If this ever changes, T2
  reopens.

## § Stack

Locked at T3. **Python on the official `mcp` SDK v2
(`mcp==2.1.1`, MIT, Python 3.10+).**

- Streamable HTTP is a one-call option: `mcp.run(transport=
  "streamable-http", host="127.0.0.1", port=<port>, stateless_http=
  True, transport_security=TransportSecurity(host="127.0.0.1"))`.
- `mcp.server.MCPServer` is the high-level façade. Tools are
  registered with `@mcp.tool(...)` and Python type hints; the SDK
  derives the JSON Schema and Pydantic validation.
- `mcp.server.auth.TokenVerifier` is the only auth integration.
- `httpx` ships transitively (`httpx2>=2.5.0`) — the SilverBullet
  client is a 20-line `httpx.AsyncClient`.
- `pydantic` ships transitively (`pydantic>=2.12.0`).

### `pyproject.toml` (target shape)

```toml
[project]
name = "mcp-silverbullet"
version = "0.1.0"
requires-python = ">=3.10"
dependencies = [
    "mcp==2.1.1",
    "httpx>=0.27",   # 2.x at runtime — uv2nix will pin exact versions
]

[project.optional-dependencies]
dev = [
    "pytest>=8",
    "pytest-asyncio>=0.23",
    "inline-snapshot>=0.10",
    "anyio>=4.4",    # for pytest
]
```

## § Tools

v1 locked three tools at T4; v1.1 grew the surface to eight (T18
added `delete_page`, T19 added `append_to_page`, T20 added
`patch_page_lines`, T21 added `patch_page_replace`, T22 added
`move_page`). v1.2's T25 adds `page_exists` for a ninth tool —
a cheap `GET /.fs/{name}` that returns `bool` instead of the full
markdown body — and T26 adds a `dry_run=True` knob to the three
read-modify-write tools (`append_to_page` / `patch_page_lines` /
`patch_page_replace`) so an agent can preview a patch without
committing (the read still happens and `if_match=<etag>` is checked
against the read's etag, but no PUT is issued; the return shape
is a different `{dry_run, original, patched, diff}` envelope).
T28 widens `list_pages`'s row shape from the v1.1 minimal
`{name, etag}` subset to the same envelope family the read/write
tools use (`{name, etag, size_bytes, last_modified_ms, created_ms}`)
and adds an opt-in per-page etag-hydration fallback
(`MCP_SILVERBULLET_LIST_PAGES_HYDRATE_ETAGS`) so an operator who
needs `if_match` round-trips from a list call can pay the N+1
cost of one GET per page. T27 adds `diff_pages(name, other_name?,
other_body?)` — a tenth tool that takes one page plus either a
second page (`other_name`) or a literal markdown string
(`other_body`) and returns a `difflib.unified_diff` between the
two bodies alongside the read-side envelopes for each page; the
shape is line-based by default (token-level / word-level diff is
a v1.3 refinement). **Ten tools, one resource template.**

| Tool | Input (Python type hint) | SB call | Returns (T23+) | Side effects |
|---|---|---|---|---|
| `read_page` | `name: str` | `GET /.fs/{name}` | `{body, etag, size_bytes, last_modified_ms}` (T24; `name` and `created_ms` dropped — caller passed `name`, reads have no create-vs-update distinction) | none |
| `page_exists` | `name: str` | `GET /.fs/{name}` (body bytes discarded) | `bool` (T25: `True` on 200, `False` on 404, `ToolError` on 5xx so "no, proceed" stays distinct from "SB is broken") | none |
| `write_page` | `name: str, content: str, if_match: Optional[str] = None` | `PUT /.fs/{name}` (body = `content`, headers `X-Source: external`, `X-Permission: "rw"`, optional `If-Match`) | `{name, etag, size_bytes, last_modified_ms, created_ms}` | may create / overwrite / refuse on `412` |
| `append_to_page` | `name: str, text: str, if_match: Optional[str] = None, dry_run: bool = False` | `GET /.fs/{name}` → `PUT /.fs/{name}` (read-modify-write; one newline separator inserted unless the existing body already ends in one; `dry_run=True` skips the PUT and returns a preview envelope) | `{name, etag, size_bytes, last_modified_ms, created_ms}` (live) or `{dry_run: True, original: str, patched: str, diff: str}` (dry-run; `diff` is a `difflib.unified_diff` of original vs patched) | may append / refuse on `412` (concurrent-write protection); `dry_run=True` raises the same 412-equivalent `ToolError` if `if_match=<stale_etag>`, so the agent sees one error shape across both paths |
| `patch_page_lines` | `name: str, start_line: int, end_line: int, new_content: str, if_match: Optional[str] = None, dry_run: bool = False` | `GET /.fs/{name}` → `PUT /.fs/{name}` (read-modify-write; lines are 1-indexed and inclusive; body split on `\\n` with trailing empty dropped; trailing newline preserved iff body had one; `dry_run=True` skips the PUT and returns a preview envelope) | `{name, etag, size_bytes, last_modified_ms, created_ms}` (live) or `{dry_run: True, original: str, patched: str, diff: str}` (dry-run) | may patch / refuse on `412`; out-of-range or inverted ranges raise `ToolError` upfront (no GET/PUT); `dry_run=True` raises the same 412-equivalent `ToolError` if `if_match=<stale_etag>` |
| `patch_page_replace` | `name: str, find: str, new_string: str, replace_all: bool = False, if_match: Optional[str] = None, dry_run: bool = False` | `GET /.fs/{name}` → `PUT /.fs/{name}` (read-modify-write; `find` is a literal substring, no regex; `replace_all=False` errors when `find` matches more than once; `find` not in body is an error; empty `find` is rejected upfront; `dry_run=True` skips the PUT and returns a preview envelope) | `{name, etag, size_bytes, last_modified_ms, created_ms}` (live) or `{dry_run: True, original: str, patched: str, diff: str}` (dry-run) | may patch / refuse on `412`; `dry_run=True` raises the same 412-equivalent `ToolError` if `if_match=<stale_etag>` |
| `move_page` | `name: str, new_name: str, if_match: Optional[str] = None` | `GET /.fs/{name}` → `PUT /.fs/{new_name}` (with `If-None-Match: *`) → `DELETE /.fs/{name}` (with `If-Match`) | `{name=destination, etag, size_bytes, last_modified_ms, created_ms}` (same-name no-op returns the source's envelope) | rename; write-then-delete so a partial failure leaves the body at the new name; destination always refuses to overwrite; `name == new_name` is a no-op; refuses on `412` (collision) or atomicity-caveat `ToolError` on the source-delete step |
| `delete_page` | `name: str, if_match: Optional[str] = None` | `DELETE /.fs/{name}` (header `X-Source: external`, optional `If-Match`) | `{name, etag, size_bytes=None, last_modified_ms=None, created_ms=None}` (DELETE doesn't echo `X-*` per the SB contract) | hard delete; refuses on `412` |
| `list_pages` | `prefix: str = ""` | `GET /.fs` then filter in Python (filter happens *before* hydration so the prefix reduces the per-page round-trip count) | `list[{name, etag, size_bytes, last_modified_ms, created_ms}]` (T28 widened from the v1.1 minimal subset; ``etag`` is `None` for every row on this SB build because the list payload omits the field — an operator who needs ``if_match`` round-trips opts in to per-page hydration via `MCP_SILVERBULLET_LIST_PAGES_HYDRATE_ETAGS`) | none |
| `diff_pages` | `name: str, other_name: Optional[str] = None, other_body: Optional[str] = None` (exactly one of `other_name` / `other_body`) | `GET /.fs/{name}` (and `GET /.fs/{other_name}` when `other_name` is given; one read per page, sequential, no writes) | `{diff: str, name: {name, body, etag, size_bytes, last_modified_ms}, other: same envelope or None}` (T27; line-based unified diff via `difflib.unified_diff`; `diff=""` when the two bodies are identical; `other=None` for the literal-string variant) | none (read-only — the tool tracks every request method and asserts only GETs were issued) |

Resource template:

- URI template: `silverbullet://page/{name}` (RFC 6570 form).
- Handler: `httpx GET /.fs/{name}`, returns the T24 acknowledgement
  envelope `{body, etag, size_bytes, last_modified_ms}` as a JSON
  object (`application/json` MIME type). v1.1 returned the body
  as a raw `text/markdown` blob; v1.2 T24 widens the resource to
  match the read tool's wire shape so a caller that gets the same
  dict from both surfaces (tool call vs context attachment) can
  treat them identically. The MCP SDK serializes the dict as
  JSON into `contents[0].text`; callers parse the JSON and read
  `body` / `etag` / `size_bytes` / `last_modified_ms` as needed.

### `X-Source: external`

Every `write_page` PUT carries this header so SilverBullet can
distinguish bridge writes from in-browser editor (`editor`) and
sync-plug (`sync`) writes in its attribution log. Handler logic in
`server/src/handlers/fs.rs`:

```rust
const VALID_WRITE_SOURCES: [&str; 3] = ["editor", "sync", "external"];
```

Anything else is ignored, never rejected. Unrecognized values just
don't get attributed. So if a v1 tool ever sends `X-Source: bridge`
instead of `external`, the write still succeeds — it just isn't
labeled in the event log. We send `external` to be honest about what
we are.

### Status-code mapping

| SB response | Tool behavior |
|---|---|
| `200 OK` | success — return body / `PageMeta` (read, write, list row) / `bool` |
| `404 Not Found` | `read_page` / `write_page` / `delete_page` / `append_to_page` / `patch_page_lines` / `patch_page_replace` / `move_page` return `ToolError("page not found: {name}")` (handler-level error → `isError=True`). `diff_pages` (T27) returns the same wording with `name` set to whichever page was missing (the first read's 404 short-circuits before the second; if the second read 404s the wording's `name` field is `other_name` so the agent can tell which side failed). The one exception: `page_exists` (T25) returns `False` rather than an error — 404 *is* the answer. A 404 on a `list_pages` per-page etag-hydration GET (T28) leaves that row's `etag` as `null` rather than failing the whole list. |
| `412 Precondition Failed` | `write_page` / `delete_page` / `append_to_page` / `patch_page_lines` / `patch_page_replace` / `move_page` (on the delete step) return `ToolError("precondition failed; check if_match/if_none_match")`. `move_page`'s destination-collision 412 gets the special-case wording (see the tool row above). A 412 on a `list_pages` per-page hydration GET (T28) leaves that row's `etag` as `null` (proxy / SB misconfig, not an agent error). `diff_pages` (T27) has no precondition surface; a 412 on one of its reads is highly unusual (a GET normally doesn't carry `If-Match`) but surfaces as the same wording if a proxy / SB misconfig triggers one. |
| `413 Body Too Large` | `write_page` / `append_to_page` / `patch_page_lines` / `patch_page_replace` / `move_page` (on the destination write) return `ToolError("body too large: limit is 4 MiB")` (the SDK's `max_request_body_size` default) |
| `5xx` | `ToolError("silverbullet error: <status>")` — including for `page_exists`, where 5xx deliberately returns an error rather than `False` so the caller can distinguish "no, proceed" from "SB is broken, don't make decisions". A 5xx on a `list_pages` per-page hydration GET (T28) leaves that row's `etag` as `null` (transient SB hiccup, not an agent error). |
| timeout | `ToolError("silverbullet request timed out")` — including the `list_pages` hydration walker (T28), which swallows per-page timeouts and leaves the row's `etag` as `null` rather than failing the whole call. |

### What we are not doing (v1)

- `search_pages` via SB-side server-side query — would need either
  client-side filtering over `list_pages`, or SB-side Space-Lua,
  neither of which we want v1. v1.1's `pages_touching_topic` is a
  direct-FS name+content search that lives behind the journal gate,
  not a server-side SB query.
- Templates for Space Lua objects (`silverbullet://lua/...`) — would
  expose `/.shell`, which is a rich and dangerous axis.
- `subscriptions/listen` channels for live file-watcher notifications.

## § SilverBullet client contract

The user's stated priority. Every tool call lands on one of three SB
endpoints. The contract is read straight from
`server/src/handlers/fs.rs` and `server/src/auth/jwt_authorizer.rs` in
the upstream SilverBullet server.

### Endpoints

| Method | Path | Body | Notable request headers | Notable response headers |
|---|---|---|---|---|
| `GET` | `/.fs` | – | – | `Content-Type: application/json`, `X-Space-Path`, body = `FileMeta[]` |
| `GET` | `/.fs/{name}` | – | `If-Modified-Since` (HTTP date), optional `X-Get-Meta`, optional `Accept: application/octet-stream` | `Content-Type: text/markdown` (or as SB recorded), `X-Created`, `X-Last-Modified`, `X-Content-Length`, `X-Permission`, `ETag`, `X-Content-Type` |
| `PUT` | `/.fs/{name}` | raw UTF-8 markdown | `Content-Type: text/markdown`, `X-Created`, `X-Last-Modified`, `X-Permission: "rw"`, `X-Content-Length`, **`X-Source: external`**, optional `If-Match: *` or `If-Match: <etag>`, optional `If-None-Match: *` to refuse overwrite | `200 OK` + the same `X-*` meta + new `ETag` for the body hash; `412` on precondition fail; `413` on body too large |
| `DELETE` | `/.fs/{name}` | – | `X-Source: external`, optional `If-Match: *` | `200 OK`/`412` |

### Error envelope

SB's `SpaceError` is rendered as the raw `e.to_string()` body with a
matching status code (see `space_error_response` in `handlers/fs.rs`).
We do not try to parse it as JSON; we map on status code only.

### Idempotency

`If-Match: *` requests that the file *must* exist. `If-None-Match: *`
requests that it must *not* exist. We expose `if_match` on
`write_page` so that Grok can issue "create if absent, refuse if
present" without clobbering.

### Auth header

`Authorization: Bearer <T>`, where `T` is the same secret the bridge
just verified. Verified by `server/src/auth/jwt_authorizer.rs` in
constant time. If `SB_AUTH_TOKEN` is empty (or `SB_USER` is unset and
no token configured), the route is unprotected — we **require** that
`SB_AUTH_TOKEN` is set in deployment.

## § Build (nix shape)

Locked at T5. **`uv2nix`** with a checked-in `uv.lock`.

### Why `uv2nix`

- nixpkgs ships `mcp==1.29.0` (v1.x, the legacy-era SDK). The 2026-07-28
  spec we committed to (T1) is supported only by v2 (we locked T3 to v2).
- v2 has a transitive dependency on **`mcp-types`** that nixpkgs **does
  not** package. Without `uv2nix`, consuming v2 would mean **two**
  bespoke derivations (`mcp` override + hand-built `mcp-types`), drifting
  independently from upstream.
- `uv2nix` (pyproject-nix/uv2nix, default branch `master`, built on
  `pyproject.nix`) ingests our `uv.lock` and overlays every transitive
  on top of nixpkgs's Python set. One lockfile, one source of truth.

### Inputs and outputs (target shape — concrete `flake.nix` is its own task)

```nix
inputs = {
  nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
  pyproject-nix = {
    url = "github:pyproject-nix/pyproject.nix";
    inputs.nixpkgs.follows = "nixpkgs";
  };
  uv2nix = {
    url = "github:pyproject-nix/uv2nix";
    inputs.pyproject-nix.follows = "pyproject-nix";
  };
  nixpkgs-python = {
    url = "github:pyproject-nix/build-system-pkgs";
    inputs.uv2nix.follows = "uv2nix";
    inputs.pyproject-nix.follows = "pyproject-nix";
    inputs.nixpkgs.follows = "nixpkgs";
  };
};

outputs = { self, nixpkgs, uv2nix, nixpkgs-python, ... }:
let
  pkgs = import nixpkgs { system = "x86_64-linux"; };
  workspace = uv2nix.lib.loadUvWorkspace { workspaceRoot = ./.; };
  overlay = workspace.mkPyprojectOverlay {
    sourcePreference = pkgs.lib.mkForce "wheel";
  };
  pythonSet = pkgs.python311.overrideScope (
    nixpkgs-python.packagesFromRequirements ./requirements.txt ++ overlay
  );
in {
  packages.x86_64-linux.default = pythonSet.mkVirtualEnv
    "mcp-silverbullet-env"
    { mcp-silverbullet = [ "pyproject-runner" ]; };

  devShells.x86_64-linux.default = pkgs.mkShell {
    packages = [ pythonSet.mcp-silverbullet ];
    inputsFrom = [ self.packages.${system}.default ];
  };
};
```

(The exact `mkVirtualEnv` argument names vary between uv2nix releases;
the contract is "run `server.py` from inside this environment".)

### Dev workflow

- `nix develop` drops you into an isolated Python env with `mcp`,
  `httpx`, `pytest`, `pytest-asyncio`, `inline-snapshot`, `anyio` —
  one lockfile, no separate `requirements.txt` for dev.
- `nix run` runs `python ./src/server.py`.
- `nix run .#checks.x86_64-linux.pytest` runs the test suite under the
  same Python env, no extra fixture config.

### Follow-ups

- Wire the actual `flake.nix` stub.
- File a PR to nixpkgs upgrading `python3Packages.mcp` from v1.29.0 to
  v2.x and adding `mcp-types`.

## § Boot & deployment

Locked at T0 (charter), T6 (lifted), T-cf-tunnel (charter). The
bridge does not provision its own tunnel.

### Boot order

1. **SilverBullet** binds `127.0.0.1:3000`, reads
   `SB_AUTH_TOKEN=<T>` from env.
2. **`mcp-silverbullet`** binds `127.0.0.1:8000` (the port is
   conventional; pick whatever the user prefers), reads
   `MCP_SILVERBULLET_TOKEN=<T>` and `MCP_SILVERBULLET_SB_URL=http://127.0.0.1:3000`
   from env.
3. **`cloudflared`** (already configured by the user per charter)
   publishes `127.0.0.1:8000` at `https://<tunnel-or-stable>.example/`.
4. The user pastes the public URL into a Grok **Custom Connector**
   dialog with the same `<T>` as the bearer token.

If the tunnel URL rotates (Cloudflare quick tunnels on free), the
token stays the same; the user re-pastes the new URL into the Grok
connector dialog. No tool calls fail during rotation; only the next
call after rotation needs the updated URL.

### Transport security

`mcp.run(streamable_http_app, transport_security=TransportSecurity(host="127.0.0.1"))`
is the default. Behind the Cloudflare tunnel, Grok connects to
`<tunnel>` which forwards to `127.0.0.1:<port>` — the host header
Grok sees is `<tunnel>`, not `127.0.0.1`. **We need to relax the
allowlist**:

```python
transport_security=TransportSecurity(host=["127.0.0.1", "<tunnel-host>"])
```

Or, if the operator prefers trust-the-tunnel and skip the host check:

```python
transport_security=None
```

Document the chosen shape in deployment.

### TLS

The bridge speaks plain HTTP to SilverBullet (`127.0.0.1:3000`) and
plain HTTP on its own port (`127.0.0.1:8000`). `cloudflared`
terminates TLS. There is **no** TLS on the bridge. Local traffic only.

## § Testing

Locked at T7. **Three `pytest` layers; no Grok, no real SilverBullet,
no Inspector UI in CI.**

### Layer 1 — in-memory tool tests

`Client(mcp, raise_exceptions=True)` connects directly to our server
object — no port, no HTTP. Tests every tool with `httpx.MockTransport`
shaping the SB backend. Sub-100ms per test. `Client(mcp)` is
**era-neutral** by default, so it exercises the modern leg of the
dual-era handler automatically.

### Layer 2 — HTTP integration on a real socket

`mcp.run(transport="streamable-http", host="127.0.0.1", port=<port>)`
in a fixture, then `Client("http://127.0.0.1:{port}/mcp")` against
the running server. Covers what Layer 1 cannot:

- `Authorization: Bearer` header parsing.
- `401 Unauthorized` + `WWW-Authenticate: Bearer resource_metadata=…`
  response shape.
- `/.well-known/oauth-protected-resource/mcp` discovery document.
- `Accept: application/json, text/event-stream` route parity.

### Layer 3 — bridge → SB request envelope

`httpx.MockTransport` substitutes for a real SilverBullet. Verifies
that `write_page` issues a PUT with `Content-Type: text/markdown`,
`X-Source: external`, the right `If-Match`, and the body matches
callers' `content`. No Rust toolchain required.

The full integration is composed in CI as Layer 2 fixture + Layer 3
mocking + `Client(http_bridge_url).call_tool(...)` driving the chain.

### Manual layer — `mcp dev` (not CI)

`uv run mcp dev src/server.py` from a terminal opens the Node-based
MCP Inspector and lets a human verify the four pieces (`read`,
`write`, `list`, resource template) end-to-end. Never in CI.

### Test catalog (v1)

See `docs/wayfinder/map.md#t7-test-surface` § "Test catalog (v1)" for
the v1 matrix.

## § Threat model (working)

- **Trusted zone**: the bridge, SilverBullet, the Cloudflare tunnel's
  local egress, and the operator's machine.
- **Untrusted zone**: the public internet, Grok's servers, and any
  party that can reach the tunnel URL.

Threats accepted:

- Anyone with the bearer token has read+write on every SilverBullet
  page the bridge can see. Mitigation: a single user behind a
  Cloudflare tunnel they control; the token is a 32-byte random;
  rotated on suspicion. If the threat model widens, T2 reopens.

Threats out of scope (v1):

- Multi-user SilverBullet (no SB user/password, only bearer).
- Audit trail of writes via SilverBullet's revision log
  (`server/src/handlers/revisions.rs`). Reachable but not surfaced in
  the bridge yet.
- Rate limiting. Reachable via Starlette middleware; not v1.

## Glossary

- **Streamable HTTP** — the modern remote transport binding in MCP,
  introduced 2025-03-26, refined 2026-07-28 (stateless).
- **HTTP+SSE** — the predecessor of Streamable HTTP. Deprecated on
  2026-07-28.
- **Bearer token** — `Authorization: Bearer <token>`, no signature,
  no expiry; identity is the token.
- **`TokenVerifier`** — Python SDK hook for the bridge to verify
  incoming bearer tokens.
- **`uv2nix`** — Nix tool that ingests `uv.lock` into nixpkgs's
  Python set.
- **Side-car** — the bridge is a separate process from SilverBullet,
  not a fork.

## Sources (consolidated)

### Spec

- [MCP transports — 2026-07-28](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports)
- [MCP Streamable HTTP — 2026-07-28](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http)
- [MCP changelog — 2026-07-28](https://modelcontextprotocol.io/specification/2026-07-28/changelog)

### Python SDK (`mcp==2.1.1`, v2-track)

- [modelcontextprotocol/python-sdk](https://github.com/modelcontextprotocol/python-sdk)
- [Running your server](https://github.com/modelcontextprotocol/python-sdk/blob/main/docs/run/index.md)
- [Add to an existing app (ASGI)](https://github.com/modelcontextprotocol/python-sdk/blob/main/docs/run/asgi.md)
- [Authorization](https://github.com/modelcontextprotocol/python-sdk/blob/main/docs/run/authorization.md)
- [The Client](https://github.com/modelcontextprotocol/python-sdk/blob/main/docs/client/index.md)
- [Testing](https://github.com/modelcontextprotocol/python-sdk/blob/main/docs/get-started/testing.md)

### SilverBullet

- [`server/src/router.rs`](https://github.com/silverbulletmd/silverbullet/blob/master/server/src/router.rs) — route table.
- [`server/src/handlers/fs.rs`](https://github.com/silverbulletmd/silverbullet/blob/master/server/src/handlers/fs.rs) — `/.fs` endpoints, `X-Source`, `If-Match`.
- [`server/src/auth/jwt_authorizer.rs`](https://github.com/silverbulletmd/silverbullet/blob/master/server/src/auth/jwt_authorizer.rs) — bearer auth.
- [`server/src/auth/config.rs`](https://github.com/silverbulletmd/silverbullet/blob/master/server/src/auth/config.rs) — `SB_AUTH_TOKEN` env var.

### Nix packaging

- [pyproject-nix/uv2nix](https://github.com/pyproject-nix/uv2nix)
- [pyproject-nix/pyproject.nix](https://github.com/pyproject-nix/pyproject.nix)
- [pyproject-nix/build-system-pkgs](https://github.com/pyproject-nix/build-system-pkgs)
- [nixpkgs `python-modules/mcp` — pinned v1.29.0](https://github.com/NixOS/nixpkgs/blob/master/pkgs/development/python-modules/mcp/default.nix)

### xAI / Grok connector

- [Custom MCP Server Tunneling](https://docs.x.ai/grok/connectors/custom-mcp-tunneling)
- [Remote MCP tools](https://docs.x.ai/developers/tools/remote-mcp)
- [Built-in / catalog / custom connectors](https://docs.x.ai/grok/connectors)

---

## Notes for the next session

What this design doc does **not** cover, and what follow-up sessions
will need:

- The concrete `flake.nix` (T5 wrote a target shape; the real
  expression with the exact `mkVirtualEnv` argument names needs to
  land in a separate task ticket).
- A PR to nixpkgs upgrading `python3Packages.mcp` to v2.x.
- A first cut at `server.py`, `client.py` for the bridge, the
  `httpx`-to-SB adapter, and the `StaticTokenVerifier`.
- A `pytest` skeleton: `tests/test_tools_in_memory.py`,
  `tests/test_http_auth.py`, `tests/test_sb_envelope.py`.
- A `README.md` documenting the boot order for a fresh checkout.

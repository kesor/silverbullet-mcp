# mcp-silverbullet

Model Context Protocol bridge between [SilverBullet](https://silverbullet.md)
and MCP clients (Grok Custom Connectors, `mcp` CLI, …). The bridge is a
side-car on loopback; it does not provision tunnels.

Architecture and threat model: [`docs/design.md`](docs/design.md).
Build map: [`docs/wayfinder/map.md`](docs/wayfinder/map.md).

## What it exposes

Three tools and one resource template:

- `read_page(name)` — markdown body
- `write_page(name, content, if_match?)` — create/update
- `list_pages(prefix?)` — names + etags (needs `GET /.fs` JSON; some SB builds 307 that URL — `read_page`/`write_page` still work)
- `silverbullet://page/{name}` — same body as `read_page`, for attaching context

Inbound MCP and outbound SilverBullet share one bearer secret by default.

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

Live pytest against a real space (T7): set `MCP_SILVERBULLET_LIVE_SB_URL`
(e.g. `http://127.0.0.1:63000`) and `MCP_SILVERBULLET_LIVE_SB_TOKEN`
(empty string is fine if SB has no auth). Unset → tests skip.

## Dev

```bash
nix develop          # editable source + pytest
pytest               # Layer 1–2, no live SB
nix flake check
```

MCP SDK is pinned at `mcp==2.1.1` (`uv.lock`). License: MIT.

# Deploying mcp-silverbullet behind Cloudflare Access

End-to-end recipe for exposing an `mcp-silverbullet` instance over the public
internet, gated by Cloudflare Access, with Managed OAuth + Dynamic Client
Registration (DCR) so any standard MCP client (Grok, Claude Desktop,
Cursor, the MCP Inspector, …) can complete the OAuth dance against
your Access app without you pre-issuing client IDs.

This guide assumes you have:

- A running SilverBullet instance on loopback (the bridge talks to it
  over `/.fs` HTTP).
- A running `mcp-silverbullet` instance on loopback, reachable on its
  own port (the default is `8000`, but this guide uses `63001` to avoid
  colliding with other services on the host).
- A domain on Cloudflare — we'll use `acme.example.com` as a placeholder.
- A Cloudflare Zero Trust account (free tier is sufficient).

Throughout, replace `acme.example.com` with your real hostname and
`acme.cloudflareaccess.com` with your team's Zero Trust origin.

## Overview

```
                            Public internet
                                  │
                                  ▼
                       ┌──────────────────────┐
                       │  Cloudflare edge /   │   Managed OAuth: AS + JWKS
                       │   Access app         │
                       └──────────┬───────────┘
                                  │  tunnel (cloudflared)
                                  ▼
                       ┌──────────────────────┐
                       │   nginx (loopback)   │   copies Cf-Access-Jwt-Assertion
                       │   on your host       │   into Authorization: Bearer,
                       └──────────┬───────────┘   rewrites Host
                                  │  loopback HTTP/1.1
                                  ▼
                       ┌──────────────────────┐
                       │  mcp-silverbullet    │   validates the JWT against
                       │   on 127.0.0.1:63001│   <team>.cloudflareaccess.com/.../certs
                       └──────────┬───────────┘
                                  │  loopback HTTP/1.1 + bearer
                                  ▼
                       ┌──────────────────────┐
                       │   SilverBullet       │
                       │   on 127.0.0.1:3000  │
                       └──────────────────────┘
```

End-to-end flow for one MCP request:

1. The client (e.g. Grok) opens `https://acme.example.com/mcp`.
2. The client gets `401` with `WWW-Authenticate: Bearer realm="OAuth",
   resource_metadata="https://acme.example.com/.well-known/cloudflare-access-protected-resource/mcp"`.
3. The client fetches `/.well-known/oauth-authorization-server` (which
   points at `https://acme.cloudflareaccess.com/.../oauth/...`).
4. The client runs standard OAuth + DCR + PKCE against the Cloudflare
   authorization server. The user logs in via Google / Okta / GitHub /
   whatever IdP you wired to Cloudflare Access.
5. Cloudflare issues an opaque Access token to the client.
6. The client sends `Authorization: Bearer <opaque-access-token>` to
   `https://acme.example.com/mcp`.
7. Cloudflare Access validates the opaque token, sees the app allows it,
   and forwards the request — but replaces the Authorization header
   with `Cf-Access-Jwt-Assertion: <signed-JWT>` (a short-lived JWT
   signed by Cloudflare's team keys).
8. cloudflared tunnels the request to your origin's nginx.
9. nginx sees the tunneled request — Host header `silverbullet.local`
   (per the tunnel's `originHostHeader`), no Authorization header
   (the JWT is in `Cf-Access-Jwt-Assertion` instead). nginx rewrites
   Host to `acme.example.com` (so the bridge's MCP transport-security
   allow-list passes) and copies `Cf-Access-Jwt-Assertion` into
   `Authorization: Bearer`.
10. mcp-silverbullet validates the JWT against Cloudflare's JWKS at
    `https://acme.cloudflareaccess.com/cdn-cgi/access/certs`,
    checks `iss == https://acme.cloudflareaccess.com` and `aud == <aud-tag>`,
    and serves the MCP request. The outbound call to SilverBullet uses
    a separate bearer (`MCP_SILVERBULLET_SB_TOKEN`); Cloudflare's
    inbound JWT is never shared with SilverBullet.

## 1. Run the bridge

The bridge defaults to JWT mode in v1.4+. Set the four env vars below
before starting it. The first three are Cloudflare-specific:

```bash
export MCP_SILVERBULLET_AUTH_MODE=jwt
export MCP_SILVERBULLET_JWT_ISSUER=https://acme.cloudflareaccess.com
export MCP_SILVERBULLET_JWT_AUDIENCE=<the-AUD-tag-from-Cloudflare>
export MCP_SILVERBULLET_JWT_JWKS_URL=https://acme.cloudflareaccess.com/cdn-cgi/access/certs
export MCP_SILVERBULLET_PORT=63001
export MCP_SILVERBULLET_SB_URL=http://127.0.0.1:3000
# public URL MCP clients will discover (used in WWW-Authenticate + the discovery doc)
export MCP_SILVERBULLET_RESOURCE_URL=https://acme.example.com/mcp
# extra Host values nginx/cloudflared might forward (comma-separated)
export MCP_SILVERBULLET_ALLOWED_HOSTS=acme.example.com
uv run mcp-silverbullet    # or with Nix: nix run .#mcp-silverbullet
```

The AUD tag is a hex string Cloudflare generates when you create the
Access application. You'll find it in the Zero Trust dashboard
(Access → Applications → your MCP app → Overview) and also via
the Cloudflare API:

```bash
curl -X GET "https://api.cloudflare.com/client/v4/accounts/<ACCOUNT_ID>/access/apps/<APP_ID>" \
  -H "Authorization: Bearer <API_TOKEN>" | jq '.result.aud'
```

## 2. Run nginx in front of the bridge

nginx handles three things the bridge can't:

1. Picks the right server block based on Host (the bridge listens on
   loopback only — it can't terminate TLS or serve multiple hosts).
2. Copies `Cf-Access-Jwt-Assertion` into `Authorization: Bearer`
   (the bridge's `BearerAuthBackend` only reads the standard
   `Authorization` header).
3. Rewrites the Host header to the externally-reachable name
   (cloudflared sets it to a local name like `silverbullet.local` so
   nginx can match the right server block; the bridge's MCP
   transport-security check then validates against the public name).

Example server block (nginx.conf):

```nginx
http {
    server {
        listen 127.0.0.1:80;
        listen 10.100.0.1:80;  # add your VPN/loopback addresses as needed
        server_name silverbullet.local;

        # Everything else goes to SilverBullet on 63000 (or wherever).
        location / {
            proxy_pass http://127.0.0.1:63000;
            proxy_http_version 1.1;
            proxy_set_header Upgrade $http_upgrade;
            proxy_set_header Connection $connection_upgrade;
            proxy_buffering off;
            include /etc/nginx/recommended-proxy_set_header-headers.conf;
        }

        # The /mcp location handles MCP traffic specifically.
        location ~ ^/mcp(/|$) {
            proxy_pass http://127.0.0.1:63001;
            proxy_http_version 1.1;
            proxy_set_header Upgrade $http_upgrade;
            proxy_set_header Connection $connection_upgrade;

            # Copy Cloudflare's validated JWT into the standard
            # Authorization header so the MCP SDK's BearerAuthBackend
            # can find it. When the variable is empty (no Cf header),
            # this sets Authorization to "Bearer " — the bridge
            # returns 401, which is the correct behavior for unauth.
            proxy_set_header Authorization "Bearer $http_cf_access_jwt_assertion";

            # cloudflared's originHostHeader rewrites Host to
            # `silverbullet.local` so nginx picks this server block.
            # The bridge's MCP transport-security check compares the
            # inbound Host against MCP_SILVERBULLET_RESOURCE_URL's host
            # (acme.example.com). Override Host back to the public
            # name so the bridge's allow-list check passes.
            proxy_set_header Host acme.example.com;

            # Streamable HTTP keeps the connection open between MCP
            # requests, so disable proxy buffering (otherwise nginx
            # will buffer the entire response before forwarding) and
            # set generous timeouts.
            proxy_buffering off;
            proxy_cache off;
            proxy_read_timeout  86400s;
            proxy_send_timeout  86400s;
            keepalive_timeout   86400s;

            # DO NOT `include /etc/nginx/recommended-proxy_set_header-headers.conf;`
            # — it sets `proxy_set_header Host $host`, which would add
            # a SECOND Host header (`acme.example.com` AND the original
            # `silverbullet.local`). h11 (the bridge's HTTP parser)
            # raises `RemoteProtocolError: Found multiple Host: headers`
            # and the bridge returns 400. Set X-Forwarded-* manually
            # here if you need them.
            proxy_set_header X-Real-IP $remote_addr;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto $scheme;
            proxy_set_header X-Forwarded-Host $host;
            proxy_set_header X-Forwarded-Server $hostname;
        }
    }
}
```

The `recommended-proxy_set_header-headers.conf` file that ships with
several common nginx setups (NixOS's `services.nginx`, Debian/Ubuntu's
`nginx-extras`, ...) includes `proxy_set_header Host $host;` by
default. On any location where you also set your own
`proxy_set_header Host …`, the include fires AFTER your directive
and adds a second Host header. **Disable the include for the `/mcp`
location** (or copy out only the headers you need, as shown above).

You can verify the config with `nginx -t` and reload with
`nginx -s reload` after editing.

## 3. Run cloudflared

cloudflared gives you a stable public hostname that tunnels to your
loopback nginx. You need:

- A Cloudflare Tunnel — either a named tunnel (recommended for
  production) or a quick tunnel (URL rotates each run; fine for
  testing).
- A CNAME record for `acme.example.com` (and a wildcard
  `*.acme.example.com` if you want one tunnel to serve multiple
  subdomains) pointing at `<tunnel-uuid>.cfargotunnel.com`.

If you're just trying things out:

```bash
cloudflared tunnel --url http://127.0.0.1:80
```

Cloudflare prints a `*.trycloudflare.com` URL. Skip ahead to step 4
and use that URL in place of `acme.example.com`.

For a stable hostname, create a named tunnel once:

```bash
# In the Cloudflare dashboard: Zero Trust → Networks → Tunnels → Create
# a tunnel named "acme-mcp" and copy the token JSON.

cloudflared service install <token.json>
# Edit /etc/cloudflared/config.yml to point at your origin:
```

The tunnel config (`config.yml`):

```yaml
tunnel: acme-mcp
credentials-file: /etc/cloudflared/<tunnel-uuid>.json

ingress:
  # The MCP app — gets the path-filtered rule, which sorts first.
  - hostname: acme.example.com
    path: /mcp
    service: http://127.0.0.1:80
    # originRequest:
    #   httpHostHeader: silverbullet.local
    # cloudflared rewrites Host to silverbullet.local so nginx's
    # server block matches. We rewrite it back to acme.example.com
    # in nginx (see step 2). Without this rewrite, nginx's default
    # server returns 444 and cloudflared sees EOF and reports
    # "Unable to reach the origin service".

  # Catch-all for the bare hostname — serves the SB SPA on the same
  # origin. Same nginx server block.
  - hostname: acme.example.com
    originRequest:
      httpHostHeader: silverbullet.local
    service: http://127.0.0.1:80

  # /.well-known/oauth-* is served by nginx reverse-proxying to
  # Cloudflare's own authorization-server endpoint — but you can
  # either route it through cloudflared too (see step 4's Access
  # app policy) or hit it directly. Easiest: let cloudflared
  # proxy it.

  # 404 catch-all.
  - service: http_status:404
```

`cloudflared tunnel run /etc/cloudflared/config.yml` after editing.

## 4. Configure Cloudflare Access

You need two Access applications:

### 4a. The MCP app (acme.example.com/mcp)

Cloudflare dashboard → Zero Trust → Access → Applications → Add an
application → Self-hosted.

| Field | Value |
|---|---|
| Name | `MCP` (or whatever) |
| Domain | `acme.example.com` |
| Path | `/mcp` |
| Identity providers | your IdP (Google, Okta, GitHub, …) |
| Policies | your existing user allow-list (e.g. `acme-team@acme.cloudflareaccess.com`) |
| **Advanced settings → OIDC → Managed OAuth** | `On` |
| **Application Audience (AUD)** | copy this — it's the `MCP_SILVERBULLET_JWT_AUDIENCE` value |

Under **Advanced settings → OIDC → Dynamic Client Registration**,
add the redirect URIs your clients use. Common entries:

- `https://grok.com/connectors-oauth-exchange-code/`
- `http://127.0.0.1:<port>/oauth/callback` (Claude Desktop loopback)
- `cursor://anysphere.cursor-mcp/oauth/callback/...`

DCR requires explicit allowlist entries — there's no wildcard option
at the team level. Add a new entry each time you adopt a new MCP
client.

### 4b. The OAuth discovery bypass

`/.well-known/oauth-authorization-server` and
`/.well-known/oauth-protected-resource/mcp` must be reachable
unauthenticated, otherwise the OAuth dance never starts (the client
needs to fetch discovery before it can ask for auth). Two options:

**Option A** (simplest): don't protect them. Add a SECOND Access
application for `acme.example.com/.well-known/oauth-*` with a
`everyone` bypass policy. cloudflared routes that path to the same
nginx (or to a separate static file). nginx needs a location that
reverse-proxies the discovery endpoint to Cloudflare's team origin
(see `docs/cloudflare-setup.md` in this repo for the snippet).

**Option B**: don't run a separate Access app; just add the discovery
paths as a bypass to your existing app's policy. Cloudflare supports
both — pick whichever fits your team policy.

## 5. End-to-end test

```bash
# From your laptop, with the Cloudflare Access CLI:
cloudflared access login https://acme.example.com/mcp
# opens browser, you log in, cookie saved

# Now an MCP request via cloudflared's CLI:
cloudflared access curl https://acme.example.com/mcp

# Should see: {"error":"invalid_token","error_description":"Authentication required"}
# (HTTP 401) — the bridge received the JWT but your curl didn't send one.

# Try with the actual MCP CLI tool, which goes through the full OAuth
# dance. The token is stored in cloudflared's session cookie:
mcp dev https://acme.example.com/mcp
```

If you see `502 Unable to reach the origin service` from cloudflared,
your nginx isn't matching the `Host` header. See step 2.

If you see `400 Bad Request` from the bridge with
`h11 rejected request … Found multiple Host: headers`, you have
the `recommended-proxy_set_header-headers.conf` include firing
*after* your custom Host directive. Remove the include from the
`/mcp` location (the snippet in step 2 already shows this).

If you see `401 invalid_token`, the JWT verifier rejected the token.
Set `MCP_SILVERBULLET_LOG_LEVEL=debug` (or `MCP_SILVERBULLET_DEBUG=1`)
and check `journalctl -u mcp-silverbullet -f` while retrying — the
bridge dumps the JWKS-fetch path, the verified claims, and the
exact validation failure (signature, iss, aud, exp, nbf).

If you see `421 Misdirected Request`, the Host header on the way to
the bridge isn't in `MCP_SILVERBULLET_ALLOWED_HOSTS`. Check both the
nginx rewrite and the cloudflared `originHostHeader`.

## Appendix: minimal Nginx + Cloudflare config files

### `nginx.conf` excerpt

```nginx
http {
    server {
        listen 127.0.0.1:80;
        server_name silverbullet.local;

        location / {
            proxy_pass http://127.0.0.1:63000;
            proxy_http_version 1.1;
            proxy_set_header Upgrade $http_upgrade;
            proxy_set_header Connection $connection_upgrade;
            proxy_buffering off;
            include /etc/nginx/recommended-proxy_set_header-headers.conf;
        }

        location ~ ^/mcp(/|$) {
            proxy_pass http://127.0.0.1:63001;
            proxy_http_version 1.1;
            proxy_set_header Upgrade $http_upgrade;
            proxy_set_header Connection $connection_upgrade;
            proxy_buffering off;
            proxy_cache off;
            proxy_read_timeout  86400s;
            proxy_send_timeout  86400s;
            keepalive_timeout   86400s;
            proxy_set_header Authorization "Bearer $http_cf_access_jwt_assertion";
            proxy_set_header Host acme.example.com;
            proxy_set_header X-Real-IP $remote_addr;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto $scheme;
            proxy_set_header X-Forwarded-Host $host;
            proxy_set_header X-Forwarded-Server $hostname;
        }
    }
}
```

### Cloudflare Access app for `.well-known/oauth-*`

If you use the `Option A` discovery bypass (step 4b), set this up:

- Name: `MCP discovery bypass`
- Domain: `acme.example.com`
- Path: `/.well-known/oauth-*`
- Decision: `Bypass` (everyone)

### cloudflared config excerpt

```yaml
tunnel: acme-mcp
credentials-file: /etc/cloudflared/<tunnel-uuid>.json

ingress:
  - hostname: acme.example.com
    path: /mcp
    service: http://127.0.0.1:80
  - hostname: acme.example.com
    originRequest:
      httpHostHeader: silverbullet.local
    service: http://127.0.0.1:80
  - service: http_status:404
```

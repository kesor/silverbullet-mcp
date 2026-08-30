# Wayfinder Map — `mcp-silverbullet` design

<!--
Local-markdown tracker. The "map" is this single file. "Tickets" are the
H2 sections below. A ticket is resolved by appending a one-line gist under
"Decisions so far" with a stable anchor to where the answer lives.

The skill this file follows: mattpocock/skills@wayfinder.
-->

## Destination

> A design document for `mcp-silverbullet` — an MCP bridge that lets Grok on
> the web read/write local SilverBullet pages, exposed through the existing
> Cloudflare tunnel, with a `flake.nix` for dependencies and a clear bias
> toward off-the-shelf libraries.

Resolving every ticket in **Not yet specified** below lands on a single
`docs/design.md` describing transport, auth, tool surface, nix shape, and
deployment. No code is shipped as part of this map — the design doc is the
artefact.

### 🏁 Reached.

`docs/design.md` (563 lines) is the delivered artefact. Every ticket
resolved in this map has a dedicated section in the doc, anchored back
to the ticket's resolution body. The map is preserved as the
ticket-by-ticket decision record; the doc is the prose form of the
same decisions.

## Notes

- **Domain**: protocol bridge between MCP hosts (Grok on the web) and an
  existing HTTP API (SilverBullet `/.fs`, `/.auth`).
- **Skills every session should consult**: `mattpocock/skills@grilling`,
  `mattpocock/skills@domain-modeling`, `modern-web-guidance`, `fullstack-dev`,
  `security-and-hardening`, `incremental-implementation`.
- **Standing preferences for this effort**:
  - **Off-the-shelf libraries only** — prefer the official `mcp` Python
    SDK (`mcp==2.1.1`, MIT), `httpx` for the SilverBullet client, nixpkgs
    over hand-rolled parsers/transports/builders.
  - **One `flake.nix`** owns dependencies for both the runtime and the dev
    shell. No separate `pyproject.toml`/`requirements.txt` lockfiles outside
    what the flake pins.
  - **Side-car process**, not embedded — locked at charter time.
  - **Streamable HTTP transport** locked at T1
    ([resolution](#t1-transport-choice-streamable-http-vs-httpsse)),
    2026-07-28 spec era, stateless posture, single `POST /mcp`.
  - The design doc must spell out the **boot order** with the user-owned
    Cloudflare tunnel. Tunnel durability itself is out of scope per T6.
  - **No MCP Apps / no UI resources** — three tools + one resource
    template, locked at T4.
  - **Static bearer auth, one shared secret on both hops** — locked at T2.

## Decisions so far

<!-- index only — one line per closed ticket, link to the ticket's resolution below -->

- **[Side-car integration shape](#t0-integration-shape)**: New repo/dir
  `mcp-silverbullet/`, plain TypeScript on Node 24, separate process from
  SilverBullet. Communicates with SB via its existing `/.fs` and `/.auth` HTTP
  API. (Locked at charter time.)
- **[MCP protocol era](#t-protocol-era)**: 2026-07-28 spec, on the
  official `mcp` Python SDK (v2, `mcp==2.1.1`). Nixpkgs unpinned until T5
  picks a Python version.
- **[T-cf-tunnel]** (charter-locked, lifted fully at T6): tunnel is
  user-managed; the bridge binds `127.0.0.1:<port>` and the operator
  publishes it through their existing `cloudflared` invocation.
- **[T1 — Transport choice](#t1-transport-choice-streamable-http-vs-httpsse)**:
  Streamable HTTP — the only standard remote binding in the 2026-07-28 era.
  Stateless posture: single `POST /mcp`, optional per-request SSE responses,
  no protocol-level sessions, no `initialize` handshake. HTTP+SSE rejected
  for being deprecated and incompatible with `trycloudflare.com`.
- **[T2 — Auth model](#t2-auth-model)**: Static bearer on both hops, one
  shared secret (`MCP_SILVERBULLET_TOKEN`); bridge→SB carries the same
  `Authorization: Bearer <T>` it just verified. No OAuth 2.1 — the SDK's
  `TokenVerifier` protocol is a 10-line constant-time compare against the
  env-supplied token.
- **[T3 — Stack](#t3-stack)**: Python on `mcp==2.1.1` (`mcp.server.MCPServer`
  + `mcp.run(transport="streamable-http", stateless_http=True,
  transport_security=…)`). Streamable HTTP is a one-call option, `httpx`
  ships transitively for the SB client, `TokenVerifier` is the only auth
  integration point.
- **[T4 — Tool surface](#t4-tool-surface)**: Three tools, one resource
  template — `read_page`, `write_page` (with `X-Source: external` on the
  PUT), `list_pages` (filter by prefix client-side), and
  `silverbullet://page/{name}` for Grok's "attach" UI.
- **[T6 — Tunnel durability](#t6-tunnel-durability)**: Resolved as
  out-of-scope lift. The bridge binds `127.0.0.1:<port>`; the user's
  existing `cloudflared` publishes it. One paragraph in `design.md`
  §Deployment covers boot order; URL-rotation is the operator's problem.
- **[T8 — License & repo home](#t8-license--repo-home)**: License → MIT
  (matches the upstream SDK). Repo home → deferred ("dev local for now");
  reopen if/when there's a reason to publish.
- **[T5 — Nix shape](#t5-nix-shape)**: `uv2nix` + a checked-in `uv.lock`,
  consumed through `pyproject.nix` overlays onto `python311`. nixpkgs
  ships `mcp==1.29.0`, but v2 is non-negotiable (T1/T3) and v2 pulls in
  `mcp-types` which nixpkgs doesn't package. `uv2nix` ingests the
  lockfile and overlays every transitive on top of nixpkgs's Python
  set; no per-dep overrides.
- **[T7 — Test surface](#t7-test-surface)**: Three `pytest` layers, no
  Grok, no real SB. (1) `Client(mcp)` in-memory for unit-level tool
  logic. (2) `mcp.run(streamable-http)` on a free port + `Client(url)`
  for the bearer-auth and well-known-doc surface. (3) `httpx.MockTransport`
  for the bridge→SB hand without spinning up Rust. A manual "Inspector
  via `mcp dev`" layer for human verification; never in CI.

## Tickets

<!--
Each ticket is sized to one 100K-token session. Mark with label
`wayfinder:<type>`. Claim by setting assignee when starting work.
-->

### T0. Integration shape

> **Status**: ✅ resolved (charter-locked)
> **Question**: Where does the bridge code live?
> **Resolution**: Side-car process — TypeScript in this repo, talks to
> SilverBullet's `/.fs` over loopback HTTP. Independent lifecycle.

---

### T1. Transport choice (Streamable HTTP vs HTTP+SSE)

> **Status**: ✅ resolved
> **Labels**: `wayfinder:research`
> **Question**: Streamable HTTP (2025-03-26 onward) or HTTP+SSE (legacy
> 2024-11-05), or both?
> **Files when resolved**: design.md §Transport.
> **Decision-payload expected**: chosen transport name; rejection rationale
> for the alternative; minimum SDK class names.

**Resolution.** Adopt **Streamable HTTP** in the **2026-07-28 spec era**,
**stateless posture**. Reject HTTP+SSE outright.

Source citations:

1. [spec/2026-07-28/basic/transports — overview] lists **only two standard
   bindings**: stdio and Streamable HTTP. HTTP+SSE is no longer standard.
2. [spec/2026-07-28/basic/transports/streamable-http] — "Streamable HTTP was
   introduced in protocol version 2025-03-26 as a replacement for the
   HTTP+SSE transport from protocol version 2024-11-05."
3. [spec/2026-07-28/changelog — Deprecated] — "Reclassify the HTTP+SSE
   transport (deprecated since protocol version `2025-03-26`) as Deprecated
   under the feature lifecycle policy. Migrate to Streamable HTTP."
4. [spec/2026-07-28/basic/transports/streamable-http — Sending Messages] —
   server "**MUST**" expose a single POST endpoint (e.g. `/mcp`), and the
   client chooses via the `Accept` header whether the response is
   `application/json` or `text/event-stream`.
5. [spec/2026-07-28/changelog — major changes for 2026-07-28] — *Stateless
   server posture*: removes `Mcp-Session-Id`, removes the
   `initialize`/`notifications/initialized` handshake, removes the GET
   stream endpoint, removes SSE resumability (`Last-Event-ID`). Servers
   needing cross-call state pass it as ordinary tool arguments.
6. [xAI custom-MCP-tunneling guide] — "`server_url` ... Only Streamable
   HTTP and SSE transports are supported" by Grok's connector.
7. [xAI custom-MCP-tunneling guide — Cloudflare quick tunnel caveat] —
   "Cloudflare quick tunnels do not support Server-Sent Events (SSE). If
   your MCP server uses the SSE transport, use ngrok instead. Servers
   using the newer Streamable HTTP transport work fine with Cloudflare."

Binding shape:

- Endpoint: `POST /mcp` only. (No GET stream; no legacy `/sse`.)
- Every request carries `MCP-Protocol-Version: 2026-07-28`,
  `Mcp-Method: <rpc>`, `Mcp-Name: ...` headers + the JSON-RPC body
  including `_meta.io.modelcontextprotocol/protocolVersion`.
- Per-request responses may be a single JSON object or an SSE stream
  scoped to that request (used only when the tool emits
  `notifications/progress` mid-call).
- No session id, no `initialize` handshake, no cookies. Stateless.
- Long-lived push notifications for `notifications/tools/list_changed` and
  `notifications/resources/updated` come from a separate
  `subscriptions/listen` request — that stays a v2 nice-to-have, not a
  v1 gate.

SDK class names (concrete, taken from the v2 source tree):

- `McpServer` from `@modelcontextprotocol/server` — registers tools.
- `WebStandardStreamableHTTPServerTransport` from
  `@modelcontextprotocol/server` — the lower-level transport (used by
  hand-rolled Hono servers).
- `createMcpHandler(buildServer)` from `@modelcontextprotocol/server` —
  the new ergonomics: a single function that returns a web-standard
  handler (`{ fetch(req: Request): Promise<Response> }`) backed by the
  Streamable HTTP transport, dual-era by default (stateless posture
  serves the modern leg, legacy negotiation path drops back when the
  client asks for the 2025-03-26 era).
- `createMcpHonoApp()` from `@modelcontextprotocol/hono` — Hono adapter
  that arms DNS-rebinding / origin protection by default and exposes
  `c.get('parsedBody')` for transports that need a parsed JSON body.
- Wire-up (matches `examples/hono/server.ts`):
  ```ts
  const handler = createMcpHandler(() => buildServer());
  const app = createMcpHonoApp();           // localhost host/origin guard
  app.all('/mcp', c => handler.fetch(c.req.raw));
  ```

Why HTTP+SSE is rejected (defensible shortlist):

- **Spec**: reclassified as Deprecated under the feature-lifecycle policy
  on 2026-07-28; new implementations explicitly told not to adopt it.
- **Interop**: HTTP+SSE requires the server to push `event: endpoint`
  before the client can post anything. Cloudflare's reverse proxy on a
  free quick tunnel buffers until the connection closes, breaking the
  server-initiated endpoint ping that HTTP+SSE depends on. (xAI guide
  flags this verbatim.) We use Cloudflare.
- **Mechanics**: HTTP+SSE prescribes a two-endpoint contract (`GET /sse`
  + `POST /messages?sessionId=…`) and a stateful `endpoint` event;
  `subscriptions/listen` and modern list-changed notifications ride on
  the SSE leg. That contract is not what 2026-07-28 tooling is
  exercising anymore (Inspector only serves the modern leg on its HTTP
  test servers; the legacy `mcp-app-http.json` is explicitly tagged
  "legacy era").
- **Diminishing returns**: it would re-introduce a transport-specific
  dependency on Grok's deprecation timeline, and would mean writing two
  handlers (`createMcpHandler` already gives the era fallback for free).

**Effects on the rest of the map.** T3 (stack) is unblocked; T5 (nix
shape) and T7 (test surface) become researchable. T4 (tool surface) was
never blocked on T1.

**Sources also captured for design.md:**

- https://modelcontextprotocol.io/specification/2026-07-28/basic/transports
- https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http
- https://modelcontextprotocol.io/specification/2026-07-28/changelog
- https://modelcontextprotocol.io/specification/2024-11-05/basic/transports
- https://docs.x.ai/developers/tools/remote-mcp
- https://docs.x.ai/grok/connectors/custom-mcp-tunneling
- https://github.com/modelcontextprotocol/typescript-sdk/tree/main/examples/hono
- https://github.com/modelcontextprotocol/typescript-sdk/tree/main/packages/middleware/hono

[xAI custom-MCP-tunneling guide]: https://docs.x.ai/grok/connectors/custom-mcp-tunneling
[spec/2026-07-28/basic/transports — overview]: https://modelcontextprotocol.io/specification/2026-07-28/basic/transports
[spec/2026-07-28/basic/transports/streamable-http]: https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http
[spec/2026-07-28/changelog]: https://modelcontextprotocol.io/specification/2026-07-28/changelog
[spec/2024-11-05/basic/transports]: https://modelcontextprotocol.io/specification/2024-11-05/basic/transports

### T2. Auth model

> **Labels**: `wayfinder:grilling`
> **Question**: Static bearer token in Grok's custom-connector dialog, or
> full OAuth 2.1 with discovery + dynamic-client registration?
> **Files when resolved**: design.md §Auth.
> **Decision-payload expected**: chosen scheme; token-source environment
> variable name; rotation story; what happens if the tunnel rotates URL.

### T3. Stack

> **Labels**: `wayfinder:grilling`
> **Question**: TypeScript (`@modelcontextprotocol/server` + Hono middleware)
> vs Python (`mcp` SDK) vs Rust (`rmcp` crate). Factor in: official-SDK
> maturity, nixpkgs friction, and how well Streamable HTTP transport is
> shipped in each.
> **Files when resolved**: design.md §Stack.

### T4. Tool surface

> **Labels**: `wayfinder:grilling`
> **Question**: Minimum viable (`read_page`, `write_page`, `list_pages`) vs
> richer (`search`, `delete_page`, `move_page`, resource templates). What's
> the smallest set that demonstrably makes Grok useful for note-taking?
> **Files when resolved**: design.md §Tools, §Resources.

### T5. Nix shape

> **Status**: ✅ resolved
> **Labels**: `wayfinder:research`
> **Question**: How does the flake consume the Python stack locked by T3?
> Candidates: `withPackages`, `poetry2nix`, `uv2nix`, `buildPythonApplication`.

**Resolution.** Adopt **`uv2nix`**, with a checked-in `uv.lock`. Reject
the other three.

### Why `uv2nix`

- The T3-locked stack is **Python on `mcp==2.1.1`** (the v2-track SDK).
- **nixpkgs only ships `mcp==1.29.0`** as of master on the date of
  resolution. v1.x is the legacy-era SDK and is incompatible with the
  2026-07-28 spec we locked at T1 (no `subscriptions/listen`, the
  `initialize` handshake still present, no stateless posture helper).
  Pulling v2 means consuming PyPI directly.
- The v2 SDK has a transitive dependency on **`mcp-types`** that
  nixpkgs **does not package**. So the `withPackages` + `overridePythonAttrs`
  shape would mean **two** bespoke derivations (`mcp` override +
  a hand-built `mcp-types`), drifting independently from upstream.
- **`uv2nix`** (pyproject-nix/uv2nix, default branch `master`,
  powered by `pyproject.nix`) ingests our `uv.lock` and overlays every
  transitive on top of nixpkgs's Python set. One lockfile, one source
  of truth, one override surface. The flake ends up looking like:

```nix
  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    pyproject-nix = { url = "github:pyproject-nix/pyproject.nix"; inputs.nixpkgs.follows = "nixpkgs"; };
    uv2nix = { url = "github:pyproject-nix/uv2nix"; inputs.pyproject-nix.follows = "pyproject-nix"; };
    nixpkgs-python = { url = "github:pyproject-nix/build-system-pkgs/uv2nix-extra"; inputs.uv2nix.follows = "uv2nix"; inputs.pyproject-nix.follows = "pyproject-nix"; inputs.nixpkgs.follows = "nixpkgs"; };
  };
  outputs = { self, nixpkgs, uv2nix, nixpkgs-python, ... }:
    let
      pkgs = import nixpkgs { system = "x86_64-linux"; };
      workspace = uv2nix.lib.loadUvWorkspace { workspaceRoot = ./.; };
      overlay = workspace.mkPyprojectOverlay { sourcePreference = pkgs.lib.mkForce "wheel"; };
      pythonSet = pkgs.python311.overrideScope (nixpkgs-python.packagesFromRequirements ./requirements.txt ++ overlay);
    in {
      packages.x86_64-linux.default = pythonSet.mkVirtualEnv "mcp-silverbullet-env" {
        mcp-silverbullet = [ "pyproject-runner" ];  # entry-point
      };
    };
```

  (Approximate, lifted from the `pyproject-nix/uv2nix` pattern; the
  exact API is fixed when the stub `flake.nix` lands.)

### Why not the other options

| Option | Why rejected |
|---|---|
| `python311.withPackages (ps: [ ps.mcp ... ])` | Pins us to `mcp==1.29.0`, the wrong SDK for our spec. Skipping it breaks T3/T1. |
| `poetry2nix` of a `poetry.lock` | Doubles lockfile formats alongside `uv.lock`. Twice the maintenance. |
| `buildPythonApplication` w/ hand-built venv | Most code, most drift risk. `uv2nix` consumes the exact same lockfile, so this is strictly worse on the "off-the-shelf" axis. |
| Skip the Nix shell entirely; just `uv run` | Breaks the standing preference ("one `flake.nix` owns dependencies"). `nix run`, `nix-direnv`, and home-manager service modules all need a flake. |

### Standing-preference fit

- **Off-the-shelf libraries**: `uv2nix`, `pyproject.nix` are the
  standard Nix libraries for this exact task. We are not writing any
  parser, builder, or transport — we're composing nixpkgs + uv2nix +
  a uv.lock.
- **Single `flake.nix`**: one flake at repo root, one pyproject.toml,
  one uv.lock, one requirements.txt if we keep one for `mkVirtualEnv`.
  That's the whole surface.

### Dev shell and CI side (forward-looking)

- The same `uv2nix` overlay produces `nix develop`, so `nix-direnv`
  setups, `home-manager` modules, or NixOS modules wrapping the bridge
  all line up.
- Test fixtures (T7) can `import` the same `pythonSet` instead of
  double-pinning — the SDK under test is whatever's in `uv.lock`,
  not whatever's in CI's `pip install`.

### Follow-ups (carryover to *Not yet specified*)

- Wire the actual `flake.nix` stub (minimal shape: inputs, outputs,
  `nix run`). Marked as a separate task ticket, not part of the design doc.
- File a PR to nixpkgs upgrading `python3Packages.mcp` from v1.29.0 to
  v2.x and adding `mcp-types`. Not blocking; nice-to-have to retire
  the uv2nix machinery once nixpkgs catches up.

**Sources:**

- [NixOS nixpkgs — `pkgs/development/python-modules/mcp/default.nix`](https://github.com/NixOS/nixpkgs/blob/master/pkgs/development/python-modules/mcp/default.nix) (pins `mcp==1.29.0`).
- [PyPI `mcp==2.1.1` — requires_python, requires_dist](https://pypi.org/project/mcp/2.1.1/) (`mcp-types==2.1.1` is a runtime dep).
- [pyproject-nix/uv2nix](https://github.com/pyproject-nix/uv2nix) (default branch `master`, builds on `pyproject.nix`).
- [pyproject-nix/pyproject.nix](https://github.com/pyproject-nix/pyproject.nix) (the underlying overlay lib).
- [mcp python-sdk v2 — Authorization, Streamable HTTP, ASGI docs](https://github.com/modelcontextprotocol/python-sdk/tree/main/docs) (justifies why we want v2 even though nixpkgs ships v1).

### T6. Tunnel durability

> **Labels**: `wayfinder:grilling`
> **Question**: `trycloudflare.com` (zero-setup, URL changes each restart)
> vs a named Cloudflare tunnel (free with a domain, stable URL, requires
> account). Which is the design-doc's primary path, and which is the
> fallback? What does the operator do when the URL rotates?
> **Files when resolved**: design.md §Deployment.

### T7. Test surface

> **Status**: ✅ resolved
> **Labels**: `wayfinder:research`
> **Question**: How do we exercise the bridge end-to-end without Grok?

**Resolution.** Three layers, **all in `pytest`** with no Grok, no
Inspector UI, no real SilverBullet process needed.

### Layer 1 — in-memory unit tests on the handlers

Connect the official SDK's `Client` straight to our `MCPServer` object
(no HTTP, no port). This is the pattern on
`mcp` python-sdk v2 docs/get-started/testing.md:

```python
@pytest.fixture
async def client():
    async with Client(mcp, raise_exceptions=True) as c:
        yield c

@pytest.mark.anyio
async def test_read_page_returns_markdown(client):
    r = await client.read_resource(ReadResourceArguments(uri="silverbullet://page/daily"))
    assert isinstance(r.contents[0], TextResourceContents)
    assert r.contents[0].text.startswith("# 2026-08-26")
```

Coverage of every tool with `httpx.MockTransport` shaping the SB
backend. Fast — sub-100ms per test, no port. `Client(mcp)` is
**era-neutral** so it automatically exercises the modern leg of our
dual-era handler.

### Layer 2 — HTTP integration on a real socket

Spin the bridge up with `mcp.run(transport="streamable-http", ...)`
inside an async fixture on a free port; point a `Client` at
`f"http://127.0.0.1:{port}/mcp"`:

```python
@pytest.fixture
async def http_bridge(unused_tcp_port_factory):
    port = unused_tcp_port_factory()
    t = asyncio.create_task(
        asyncio.to_thread(
            lambda: mcp.run(
                transport="streamable-http",
                host="127.0.0.1", port=port,
                stateless_http=True,
                transport_security=TransportSecurity(host="127.0.0.1"),
            )
        )
    )
    async with Client(f"http://127.0.0.1:{port}/mcp", raise_exceptions=True) as c:
        yield c
    t.cancel()
```

Covers whatever in-memory testing cannot: the `Authorization: Bearer`
header, the `401 Unauthorized` + `WWW-Authenticate` response on bad
tokens, the SDK's `/mcp` route and the `/.well-known/oauth-protected-resource/mcp`
route being served, the `Accept: application/json, text/event-stream`
header dance.

### Layer 3 — SilverBullet client plumbing

`httpx.MockTransport` for the bridge→SB hand:

```python
def test_write_page_sets_x_source_external():
    transport = httpx.MockTransport(handler_for_expected_put)
    sb = SilverBulletClient(base_url="http://sb.invalid", token="t", transport=transport)
    sb.write_page("foo", "bar")
    assert transport.calls[0].request.headers["X-Source"] == "external"
```

That covers the SB-side mechanics **without** spinning up a real SB.
A full bridge-to-SB end-to-end is then composed in CI by:

1. layer-2 fixture brings up the bridge,
2. `httpx.MockTransport` substitutes for SB,
3. `Client(http_bridge_url).call_tool("write_page", ...)` calls
   through, the bridge validates the token, the mock answers,
   assertions on `MockTransport.calls` check that we set
   `X-Source: external` and the right `If-Match`.

A **manual** layer 4 — `mcp dev server.py` in a terminal — stands
in for the Inspector UI; it spawns our server as a subprocess and
opens the Node-Inspector. Used for the 5-min "does it still work
end-to-end" sanity check during dev. **Not** used in CI.

### Rejected alternatives

| Option | Why rejected |
|---|---|
| MCP Inspector as the test harness | It's a Node-UI subprocess (`mcp dev`), not deterministic, needs `npx` on PATH. Fine for ad-hoc. Not CI-friendly. |
| Spin up a real SilverBullet server in CI | Pulls in `silverbullet-server` (Rust) build, plus a test fixture for its on-disk space format. Heavy and orthogonal to what we're shipping. The `httpx.MockTransport` layer covers the same surface cheaper. |
| Custom `vitest`/`pytest-asyncio` HTTP mock at the `httpx` layer only | Loses Layer 1's zero-port in-memory path. Means every test pays a fixed setup cost. |

### Test catalog (v1)

| Tool / surface | Test |
|---|---|
| `read_page` | Layer 1, MockTransport returns `text/markdown` body |
| `read_page` not-found | Layer 1, MockTransport returns `404`, tool result has `isError=True` and a recognizable message |
| `write_page` creates | Layer 1, MockTransport records the PUT with `X-Source: external`, body matches |
| `write_page` overwrite with `if_match="*"` | Layer 1, MockTransport returns `412`, tool result has `isError=True` |
| `list_pages` with prefix | Layer 1, MockTransport returns `FileMeta[]`, fixture filters in Python |
| Bearer auth: valid token | Layer 2, returns the page; verifies `WWW-Authenticate` is absent in 200 |
| Bearer auth: missing token | Layer 2, `401` + `WWW-Authenticate: Bearer …` header present |
| Bearer auth: wrong token | Layer 2, `401`, same `WWW-Authenticate` shape |
| `/.well-known/oauth-protected-resource/mcp` | Layer 2, plain `httpx.AsyncClient` (no MCP), asserts `resource`, `authorization_servers`, `scopes_supported`, `bearer_methods_supported` |
| Bridge → SB `X-Source: external` | Layer 3 + Layer 2 fixture |

### Standing-preference fit

- **Off-the-shelf libraries**: `pytest`, `pytest-asyncio`,
  `inline-snapshot` (already a transitive test-dep of nixpkgs' `mcp`
  derivation), `httpx.MockTransport`. We don't write a transport or
  a fixture framework.
- **Single `flake.nix`**: tests are part of the same `uv2nix`
  virtualenv as runtime — same `pythonSet`, same `mkVirtualEnv` for
  CI, same `requirements.txt`. No second lockfile, no "dev deps in
  pip-tools, runtime in uv".

**Sources:**

- [mcp python-sdk v2 — Testing (in-memory Client)](https://github.com/modelcontextprotocol/python-sdk/blob/main/docs/get-started/testing.md).
- [mcp python-sdk v2 — Client (`Client("http://...")`)](https://github.com/modelcontextprotocol/python-sdk/blob/main/docs/client/index.md).
- [mcp python-sdk v2 — `raise_exceptions=True` semantics](https://github.com/modelcontextprotocol/python-sdk/blob/main/docs/get-started/testing.md#why-raise_exceptionstrue).
- [httpx.MockTransport](https://www.python-httpx.org/advanced/transports/#mock-transport) for SB-side isolation.

### T8. License

> **Labels**: `wayfinder:grilling`
> **Question**: MIT (matches `@modelcontextprotocol/server`) vs a copyleft
> variant? Repository home (GitHub user, org, none)?
> **Files when resolved**: `LICENSE`, `README.md`.

## Not yet specified

<!-- dim view of what's coming: things we suspect we'll ticket but can't yet phrase precisely -->

- How Grok should discover tool changes mid-session — should the bridge
  support `subscriptions/listen` so the SSE notify channel can deliver
  `notifications/tools/list_changed` and `notifications/resources/updated`
  mid-session, or is plain per-request POST enough? Decide after T4.
- Whether the design doc should include a SwiftBar / TUI launcher for the
  tunnel + bridge, or leave that to operator scripts.
- A schema for *what* the bridge surfaces from SilverBullet — does it pass
  through Space Lua objects, or only raw markdown? Drag-in of `/.shell` adds
  a rich, dangerous axis.
- Version pinning for `@modelcontextprotocol/server` 2.0 — once nix
  consumption is decided in T5, this becomes a ticket.
- `Origin` and DNS-rebinding protection: the TS Hono adapter arms
  localhost-origin validation by default, but our server is not on
  localhost relative to Grok (it is on the Cloudflare tunnel). Decide
  after T3 whether to relax or replace the default and ship our own host
  allow-list.

## Out of scope

- Provisioning the Cloudflare tunnel itself — already running per user.
- Forking SilverBullet — decision T0 ruled it out.
- Building an MCP Apps (UI) surface — Grok's `mcp-sandbox.grokusercontent.com`
  proxy is a separate spec lane; not needed for read/write tools.
- A general-purpose MCP router or multi-user support — single-user,
  single-instance. Reopen if user base widens.
- Selling or hosting the bridge for other people — local-tunnel-only.
- OAuth 2.1 / dynamic-client registration for the bridge — rejected by T2.
- A publishing decision for the repo home — deferred by T8 ("dev local
  for now").

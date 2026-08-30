# Wayfinder Map — `mcp-silverbullet` build

<!--
Local-markdown tracker. The "map" is this single file. "Tickets" are the
H2 sections below. A ticket is resolved by appending a one-line gist under
"Decisions so far" with a stable anchor to where the answer lives.

The skill this file follows: mattpocock/skills@wayfinder.

Prior map (the design doc) reached destination `docs/design.md` and is
preserved in git history at commit `542f1`. This map's destination is
**building the bridge the design doc describes** — code, flake, tests,
README — not the design itself.
-->

## Destination

> A runnable `mcp-silverbullet` that Grok on the web can talk to through
> the user's existing Cloudflare tunnel: code in `src/mcp_silverbullet/`,
> a `flake.nix` that builds it (`nix run` works from a clean checkout),
> a `pytest` suite that passes (including a live-SB end-to-end test
> against the running SilverBullet on this dev box when env-gated), and
> a `README.md` documenting the boot order.

The artefact is **this repo, runnable end-to-end**. No new design
decisions to lock — those were settled in the prior map; the input here
is `docs/design.md`. Open tickets resolve the **work**, not the design.

### 🏁 Status: in flight.

Three of eight tickets resolved (T1, T2, T3). Frontier is T4
(`verifier.py` + `server.py`), T5, T6, T7, T8.

## Notes

- **Domain**: protocol bridge implementation; the design decisions are
  in `docs/design.md` (T0–T8 from the prior map). This map's tickets
  implement those decisions; they don't re-litigate them.
- **Skills every session should consult**: `mattpocock/skills@grilling`,
  `mattpocock/skills@domain-modeling`, `incremental-implementation`,
  `modern-web-guidance`, `security-and-hardening`, `huggingface-local-models`
  (only for the dev-shell `nix run` shape, if helpful — else skip).
- **Standing preferences for this effort**:
  - **Off-the-shelf libraries only** — `mcp==2.1.1`, `httpx`, Starlette,
    uvicorn, pytest. We do not write transports, verifiers, or HTTP
    adapters from scratch when the SDK already ships them.
  - **One `flake.nix`** owns dependencies for both runtime and dev shell.
    Locked at T5 of the prior map (`uv2nix` + checked-in `uv.lock`).
  - **MCP version pinned to `==2.1.1`** — no `>=` ranges, no chasing
    SDK releases. uv2nix ingests `uv.lock`; the resolution is the
    lockfile.
  - **Side-car process** running on `127.0.0.1:8000` by default, behind
    the user's existing Cloudflare tunnel — never re-implementing what
    the tunnel already does.
  - **Bearer auth on both hops, one shared secret** — locked at T2 of
    the prior map. `MCP_SILVERBULLET_TOKEN` (inbound) →
    `Authorization: Bearer <T>` (outbound).
  - **Three tools + one resource template** — locked at T4. No
    `delete_page`, no `search_pages`, no Space Lua, no MCP Apps.
  - **Live-SB end-to-end test** is **env-gated** — both
    `MCP_SILVERBULLET_LIVE_SB_URL` and `MCP_SILVERBULLET_LIVE_SB_TOKEN`
    must be set, otherwise the test skips with a clear message.
    CI stays green; the dev box exercises the live path.
  - **Ready for nginx-fronted deployment** — `transport_security`
    allows hosts via `MCP_SILVERBULLET_ALLOWED_HOSTS` (default
    `127.0.0.1`; operator extends with `<mcp>.local` or
    `<tunnel>.trycloudflare.com` when going through nginx or
    Cloudflare). No code change needed when the day comes.
  - **MIT license**, matches the upstream SDK (T8 of the prior map).
- **Operator environment on this dev box** (for the live-SB test):
  SilverBullet is on `127.0.0.1:63000` with no auth token in this
  session (per `ps` output). The live-SB test must point its env vars
  at that URL and (if SB requires a token) set the matching one.

## Decisions so far

<!-- index only — one line per closed ticket, link to the ticket's resolution below -->

- [T1. Repo skeleton + pyproject.toml](#t1-repo-skeleton--pyprojecttoml) (commit `dd478`): package shell at `src/mcp_silverbullet/` with smoke entry point, `pyproject.toml` pinning `mcp==2.1.1` + `httpx2>=2.5.0` + Starlette + uvicorn (plus `pytest`/`pytest-asyncio`/`respx` as the test extra), stub `README.md` (T6 replaces), `uv.lock` resolved against Python 3.13. `uv sync` resolves 42 packages; `python -m mcp_silverbullet` prints hello and exits 0.
- [T2. `flake.nix` (uv2nix + pinned commits)](#t2-flakenix-uv2nix--pinned-commits) (commit `9aabc`): `flake.nix` consumes `uv.lock` via `pyproject-nix/build-system-pkgs` + `uv2nix` (the actual API, NOT the design doc's pseudocode — see resolution); four inputs pinned via `flake.lock` to known commits (nixpkgs `56c02bc...`, pyproject.nix `1b14855...`, uv2nix `4b59ab...`, build-system-pkgs `90ffde...`, all 2026-08). Python interpreter pinned to `pkgs.python313` (3.13.15) so the runtime venv and `uv.lock` agree on wheels (nixpkgs's `pkgs.python3` had drifted to 3.14). `packages.${system}.default` builds `mcp-silverbullet-env` from `workspace.deps.default` (no pytest/editables); `devShells.${system}.default` uses `mkEditablePyprojectOverlay` + `workspace.deps.all` for live-source development; `checks.${system}.pytest` is the uv2nix `passthru.tests` pattern — overrides the `mcp-silverbullet` package to add a derivation that runs `pytest` against `lib.cleanSource ./.` (whole repo minus .venv). Runtime venv smoke (`mcp==2.1.1`, `httpx2==2.12.0`, `python -m mcp_silverbullet` → "hello from mcp-silverbullet") and `nix flake check` both green on `x86_64-linux`. The devShell required two `pyproject.toml` carry-forwards: a `dev` dependency-group containing `editables` (so `mkEditablePyprojectOverlay` includes it in `workspace.deps.all`), and `editables` listed in `[build-system] requires` (so hatchling's `build_editable` finds it at build time, not just at install time). 16 Layer-3 tests pass in both `nix flake check` and `nix develop`.
- [T3. `sb_client.py` (httpx adapter for /.fs)](#t3-sb_clientpy-httpx-adapter-for-fs) (commit `de944`): outbound bridge half — `SBClient` with `read_page`/`write_page`/`list_pages` against SB's `/.fs/...`, typed exceptions (`PageNotFound`, `PreconditionFailed`, `BodyTooLarge`, `ServerError`) per the § Tools status-code table, `FileMeta` dataclass (name + etag only), `write_page` PUT carries `X-Source: external` + `X-Permission: rw` + explicit `Content-Type: text/markdown` (httpx2 doesn't auto-set it for `content=str`); `If-Match` / `If-None-Match: *` both wired, `if_match` wins if both are passed. 16 Layer-3 tests under `httpx.MockTransport` (no real SB), all green in 0.07s; T1 smoke unbroken. Write-envelope fog (T8) deliberately not resolved here — `write_page` only sends the two headers the design doc requires, the real-write attempt is what decides the rest.

## Tickets

<!--
Each ticket is sized to one 100K-token session. Mark with label
`wayfinder:<type>`. Claim by setting an `Assignee:` line at the top
of the ticket's block (no real "assignee" field exists in this local
tracker; the line IS the claim — concurrent sessions skip any ticket
that already has one).
Tickets wire blocking edges in a second pass (the tracker is a single
file; "blocking" is rendered by ticket ordering and an explicit
"Blocks:" line per ticket).
-->

### T1. Repo skeleton + pyproject.toml

> **Labels**: `wayfinder:task`
> **Type**: AFK (the agent does it)
> **Assignee**: claude (claimed 2026-01-15, resolved same day)
> **Status**: ✅ resolved (commit `dd478`)
> **Question**: Lay down the directory layout, `pyproject.toml` with
> pinned deps, an empty `src/mcp_silverbullet/` package, a `uv.lock`
> generated via `uv lock`, a `.gitignore`, and the smoke-test entry
> point (`python -m mcp_silverbullet` prints a hello and exits).
> **Files when resolved**: `pyproject.toml`, `uv.lock`, `.gitignore`,
> `src/mcp_silverbullet/__init__.py`, `src/mcp_silverbullet/main.py`,
> `src/mcp_silverbullet/__main__.py`.
> **Done when**: `uv sync` produces a venv with `mcp==2.1.1` and
> `httpx>=0.27` resolved, `python -m mcp_silverbullet` prints a hello
> and exits 0, and `git ls-files` shows the new files.
> **Resolution**: `mcp==2.1.1` and `httpx2==2.12.0` (a.k.a. `httpx2>=2.5.0`) resolved; `python -m mcp_silverbullet` prints hello and exits 0; venv built against CPython 3.13.15 (the nixpkgs default). See commit `dd478` for the full diff and the carrying-forward notes (sb_client.py will import from httpx2, not httpx; FastMCP is now MCPServer in v2.x).
> **Unblocks**: T2, T3, T4.

---

### T2. `flake.nix` (uv2nix + pinned commits)

> **Labels**: `wayfinder:task`
> **Type**: AFK (the agent does it)
> **Assignee**: claude (claimed 2026-08-26, resolved same day)
> **Status**: ✅ resolved (commit `9aabc`)
> **Question**: Wire `flake.nix` to consume the `uv.lock` from T1 via
> `uv2nix` + `pyproject-nix/build-system-pkgs`. Inputs pinned to known
> commits/tags (not `master`). Expose `packages.<system>.default`,
> `devShells.<system>.default`, and `checks.<system>.pytest`.
> **Files when resolved**: `flake.nix`, `flake.lock`, optionally a
> trimmed `requirements.txt` if `mkVirtualEnv` needs it.
> **Done when**: `nix build` produces a runnable bridge venv,
> `nix develop` drops into a shell where `python -c "import mcp"` works
> and `mcp==2.1.1` is reported, and `nix flake check` is green.
> **Blocks on**: T1.
> **Unblocks**: T5 (test runs), T6 (operator smoke run).
> **Resolution**: see commit `9aabc`. Three carry-forwards worth flagging for future flake work: (1) the design doc's pseudocode (§ Build) names the input `nixpkgs-python`; the actual repo is `pyproject-nix/build-system-pkgs`, exposed as `pyproject-build-systems` in our flake. (2) The API is `uv2nix.lib.workspace.loadWorkspace`, not `uv2nix.lib.loadUvWorkspace`. (3) Hatchling's editable-install path needs `editables` both in `[build-system] requires` (build time) AND in a `dev` dependency-group (runtime of the editable install); missing either fails the devShell build. `nix flake check` is green; `nix develop` opens a shell where `pytest` collects 16 tests and `mcp-silverbullet` runs. Devshell source-tree mode is the right shape for T4 (server.py will edit-import sb_client.py without a rebuild).

---

### T3. `sb_client.py` (httpx adapter for /.fs)

> **Labels**: `wayfinder:task`
> **Type**: AFK
> **Assignee**: claude (claimed 2026-01-15, resolved same day)
> **Status**: ✅ resolved (commit `de944`)
> **Question**: Implement the 20-line `httpx.AsyncClient` adapter
> described in `docs/design.md` § SilverBullet client contract:
> `read_page(name)`, `write_page(name, content, if_match=None)`,
> `list_pages()` — each translating HTTP status codes to typed
> exceptions per the §Tools status-code mapping.
> **Files when resolved**: `src/mcp_silverbullet/sb_client.py`.
> **Done when**: `pytest tests/test_sb_client.py` passes against
> `httpx.MockTransport` (no real SB needed; Layer 3 of the prior
> map's test surface). All three endpoints covered; all five status
> codes (`200`, `404`, `412`, `413`, `5xx`) covered for `write_page`;
> `X-Source: external` header asserted on PUT.
> **Blocks on**: T1.
> **Unblocks**: T4, T5, T7.

---

### T4. `verifier.py` + `server.py` (the MCP tools)

> **Labels**: `wayfinder:task`
> **Type**: AFK
> **Assignee**: claude (claimed 2026-08-27, resolving now)
> **Question**: Implement the `StaticTokenVerifier` (10-line
> `hmac.compare_digest` against `MCP_SILVERBULLET_TOKEN`) and the
> `MCPServer` with three `@mcp.tool(...)` registrations
> (`read_page`, `write_page`, `list_pages`) plus the
> `silverbullet://page/{name}` resource template. Tools call into
> `sb_client` and translate SB exceptions to `ToolError`.
> **Files when resolved**: `src/mcp_silverbullet/verifier.py`,
> `src/mcp_silverbullet/server.py`.
> **Done when**: `pytest tests/test_tools_in_memory.py` passes
> (Layer 1 of the prior map: `Client(mcp)` in-memory, no HTTP).
> Tools call the mocked SB client; `isError=True` on `404`/`412`/
> `413`; resource template returns `text/markdown`.
> **Blocks on**: T3.
> **Unblocks**: T5 (Layer 2 HTTP tests).

---

### T5. HTTP integration tests (auth + discovery doc)

> **Labels**: `wayfinder:task`
> **Type**: AFK
> **Question**: Stand the bridge up on a free port via
> `mcp.streamable_http_app()` + uvicorn, point `Client(url)` at it,
> and verify: bearer auth happy path; missing-token → `401` +
> `WWW-Authenticate: Bearer resource_metadata=…`; wrong token → same
> shape; `/.well-known/oauth-protected-resource/mcp` discovery doc
> is served with the right fields (`resource`,
> `authorization_servers`, `scopes_supported`,
> `bearer_methods_supported`); `Accept: application/json,
> text/event-stream` route parity.
> **Files when resolved**: `tests/test_http_auth.py`.
> **Done when**: `pytest tests/test_http_auth.py` passes; covered
> in the matrix in `docs/design.md` § Test catalog (v1).
> **Blocks on**: T2, T4.
> **Unblocks**: T7 (live-SB end-to-end).

---

### T6. Operator smoke run + README

> **Labels**: `wayfinder:task`
> **Type**: AFK (agent runs it locally and reports; no human in the
> loop)
> **Question**: Run `nix run .#mcp-silverbullet` against the running
> SilverBullet on `127.0.0.1:63000`, point `mcp dev` at it, manually
> exercise `read_page` / `write_page` / `list_pages` (create one
> page, list, read it back, delete it manually). Capture the
> transcript in the ticket resolution. Document the boot order in
> `README.md`: generate token, set env vars on both sides, run the
> bridge, point `cloudflared` at it, paste URL + token into Grok.
> **Files when resolved**: `README.md`.
> **Done when**: `nix run` starts the bridge, the manual `mcp dev`
> walkthrough succeeds, `README.md` exists with the boot order and
> references `docs/design.md` for the architectural why.
> **Blocks on**: T2.
> **Unblocks**: (none — this is the operator-facing milestone).

---

### T7. Live-SB end-to-end test (env-gated)

> **Labels**: `wayfinder:task`
> **Type**: AFK
> **Question**: Write `tests/test_e2e_live_sb.py` that reads
> `MCP_SILVERBULLET_LIVE_SB_URL` and `MCP_SILVERBULLET_LIVE_SB_TOKEN`
> from env; if either is unset, skip with a clear message; otherwise
> spin the bridge up on a free port, point a real `httpx` client at
> the live SB on this dev box (`127.0.0.1:63000`), and exercise the
> full chain: `write_page` → `read_page` roundtrip → `list_pages`
> filter → `412` on `If-Match: *` against an existing page. The test
> cleans up the page it creates (a known-name marker like
> `e2e-mcp-silverbullet-marker.md`).
> **Files when resolved**: `tests/test_e2e_live_sb.py`.
> **Done when**: with env vars set, `pytest tests/test_e2e_live_sb.py`
> passes; with env vars unset, it skips cleanly. The marker file is
> removed on success and on failure (best-effort `try/finally`).
> **Blocks on**: T5.
> **Unblocks**: (none — destination milestone).

---

### T8. Write-envelope fog (X-* headers on PUT)

> **Labels**: `wayfinder:grilling`
> **Type**: HITL (operator decides when first write fails)
> **Question**: When `write_page` actually runs against a real SB,
> do we send `X-Permission: rw`? `X-Created`? `X-Last-Modified`?
> `X-Content-Length`? The design doc enumerates them; the user punted
> this until integration.
> **Resolution will land on**: a code change to `sb_client.py`'s
> PUT envelope after a real write attempt surfaces what's needed
> vs. optional. The fog here is *which* headers end up in the v1
> envelope; the *destination* (a working bridge) doesn't change.
> **Done when**: the user has run a real write once and confirmed
> which headers survive.

## Not yet specified

<!-- dim view of what's coming: things we suspect we'll ticket but can't yet phrase precisely -->

- `subscriptions/listen` for `notifications/tools/list_changed` and
  `notifications/resources/updated` mid-session. Not v1. Specifiable
  once a real operator session demonstrates the use case.
- Whether the bridge should expose `/healthz` (cheap, fits under the
  Starlette router) — natural follow-up to T2 if we want a deploy
  probe.
- Whether the live-SB end-to-end test (T7) should *also* exercise
  `If-Modified-Since` (read with caching) and `If-None-Match: *`
  (create-only writes). Specifiable after T7 first runs.
- Long-term MCP version-pin policy: when to relax `==2.1.1` to a
  range. Punt to a future map.
- PR to nixpkgs upgrading `python3Packages.mcp` to v2.x and adding
  `mcp-types`. Nice-to-have; not blocking; punt to a future map.

## Out of scope

<!-- Work ruled beyond this map's destination. Closed/fog items go in
"Decisions so far" or "Not yet specified" respectively; this section
is for *scope* boundaries. -->

- **Provisioning Cloudflare tunnels or any reverse-proxy infrastructure**
  — operator work per T6 of the prior map. The bridge is configurable
  to sit behind one (via `MCP_SILVERBULLET_ALLOWED_HOSTS`); it does
  not provision one.
- **MCP Apps / UI resources** — Grok's `mcp-sandbox.grokusercontent.com`
  proxy is a separate spec lane; not needed for read/write tools
  (locked at T4 of the prior map).
- **Multi-user SilverBullet, OAuth 2.1, dynamic-client registration,
  per-user auth scoping** — locked out at T2 of the prior map. Single
  user, single token.
- **Selling or hosting the bridge for other people** — local-tunnel-only
  (locked at T6 of the prior map).
- **Re-deciding design questions locked in `docs/design.md`** — if a
  build ticket reveals a design issue, reopen the relevant prior-map
  ticket (T0–T8); don't re-chart.
- **Production hardening** (rate limiting, audit-trail surfacing of SB
  revisions, structured logging, Prometheus metrics) — flagged as
  "reachable but not v1" in `docs/design.md` § Threat model. Punt to a
  future map.
- **A publishing decision for the repo home** — deferred by T8 of the
  prior map ("dev local for now").

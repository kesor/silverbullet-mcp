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

### 🏁 Status: destination shifted (again).

T1–T8 resolved (runnable bridge). T9 closed as superseded:
the operator redirected to a *direct-FS journal surface*
instead of a single search tool, with the surface explicitly
gated by configuration so deployments without FS access
degrade gracefully. New tickets T10–T13 define the
foundation (config gate), read tools (histogram / tag_summary
/ recent_pages), search (`pages_touching_topic`), and a
live-space test. The new destination is **the bridge +
gated journal surface** (T10–T13).

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
    `delete_page`, no Space Lua templates (`silverbullet://lua/...`),
    no MCP Apps. **`pages_touching_topic` (T12) is the
    journal-surface search tool, not a `/.fs`-backed search tool**;
    it lives behind the journal-tools gate (T10) and is read-only.
  - **Live-SB end-to-end test** is **env-gated** — both
    `MCP_SILVERBULLET_LIVE_SB_URL` and `MCP_SILVERBULLET_LIVE_SB_TOKEN`
    must be set, otherwise the test skips with a clear message.
    CI stays green; the dev box exercises the live path.
  - **Ready for nginx-fronted deployment** — `transport_security`
    allows hosts via `MCP_SILVERBULLET_ALLOWED_HOSTS` (default
    `127.0.0.1`; operator extends with `<mcp>.local` or
    `<tunnel>.trycloudflare.com` when going through nginx or
    Cloudflare). No code change needed when the day comes. T5 will
    verify the rendered shape against the v2.1.1 SDK's
    `TransportSecuritySettings` (which exposes `allowed_hosts: list[str]`,
    not the v1-era `host=...` kwarg the design doc shows).
  - **`AuthSettings` requires both `issuer_url` and `resource_server_url`**
    to enable the bearer-auth middleware. v1 points both at the
    resource URL (no separate authz server); the discovery doc will
    therefore advertise `authorization_servers=[<resource_url>]`.
    T5 will verify the rendered shape on a real HTTP socket.
  - **MIT license**, matches the upstream SDK (T8 of the prior map).
  - **Journal tools are an optional, gated surface.** The bridge
    may run on a host that does *not* have direct access to the SB
    space directory (e.g., sidecar in a container without a volume
    mount). T10–T13 require two new env vars
    (`MCP_SILVERBULLET_SPACE_PATH` and `MCP_SILVERBULLET_JOURNAL_TOOLS`)
    to expose the journal tools; missing or unreadable
    `space_path` means the bridge boots cleanly without them and
    the existing `/.fs`-backed tools continue to work unchanged.
    This is a deliberate deployment-shape assumption the prior map
    did not make; v1 was loopback-HTTP-only.
  - **The journal tools are read-only.** No write-path through the
    journal surface. Writes continue to flow through `write_page`
    via `/.fs` so SB's own attribution log captures them.
  - **The journal surface reads files SB itself owns.** On this
    dev box that's `/var/lib/silverbullet/`, which contains
    `.cache/` and `.git/` in addition to page markdown. The journal
    tools restrict themselves to `*.md` files at the top level
    and below; they do not enumerate hidden directories. The
    `prefix` argument is validated (no `..`, no leading `/`) so
    path traversal isn't possible.
- **Operator environment on this dev box** (for the live-SB test):
  SilverBullet is on `127.0.0.1:63000` with no auth token in this
  session (per `ps` output). The live-SB test must point its env vars
  at that URL and (if SB requires a token) set the matching one.

## Decisions so far

<!-- index only — one line per closed ticket, link to the ticket's resolution below -->

- [T1. Repo skeleton + pyproject.toml](#t1-repo-skeleton--pyprojecttoml) (commit `dd478`): package shell at `src/mcp_silverbullet/` with smoke entry point, `pyproject.toml` pinning `mcp==2.1.1` + `httpx2>=2.5.0` + Starlette + uvicorn (plus `pytest`/`pytest-asyncio`/`respx` as the test extra), stub `README.md` (T6 replaces), `uv.lock` resolved against Python 3.13. `uv sync` resolves 42 packages; `python -m mcp_silverbullet` prints hello and exits 0.
- [T2. `flake.nix` (uv2nix + pinned commits)](#t2-flakenix-uv2nix--pinned-commits) (commit `9aabc`): `flake.nix` consumes `uv.lock` via `pyproject-nix/build-system-pkgs` + `uv2nix` (the actual API, NOT the design doc's pseudocode — see resolution); four inputs pinned via `flake.lock` to known commits (nixpkgs `56c02bc...`, pyproject.nix `1b14855...`, uv2nix `4b59ab...`, build-system-pkgs `90ffde...`, all 2026-08). Python interpreter pinned to `pkgs.python313` (3.13.15) so the runtime venv and `uv.lock` agree on wheels (nixpkgs's `pkgs.python3` had drifted to 3.14). `packages.${system}.default` builds `mcp-silverbullet-env` from `workspace.deps.default` (no pytest/editables); `devShells.${system}.default` uses `mkEditablePyprojectOverlay` + `workspace.deps.all` for live-source development; `checks.${system}.pytest` is the uv2nix `passthru.tests` pattern — overrides the `mcp-silverbullet` package to add a derivation that runs `pytest` against `lib.cleanSource ./.` (whole repo minus .venv). Runtime venv smoke (`mcp==2.1.1`, `httpx2==2.12.0`, `python -m mcp_silverbullet` → "hello from mcp-silverbullet") and `nix flake check` both green on `x86_64-linux`. The devShell required two `pyproject.toml` carry-forwards: a `dev` dependency-group containing `editables` (so `mkEditablePyprojectOverlay` includes it in `workspace.deps.all`), and `editables` listed in `[build-system] requires` (so hatchling's `build_editable` finds it at build time, not just at install time). 16 Layer-3 tests pass in both `nix flake check` and `nix develop`.
- [T3. `sb_client.py` (httpx adapter for /.fs)](#t3-sb_clientpy-httpx-adapter-for-fs) (commit `de944`): outbound bridge half — `SBClient` with `read_page`/`write_page`/`list_pages` against SB's `/.fs/...`, typed exceptions (`PageNotFound`, `PreconditionFailed`, `BodyTooLarge`, `ServerError`) per the § Tools status-code table, `FileMeta` dataclass (name + etag only), `write_page` PUT carries `X-Source: external` + `X-Permission: rw` + explicit `Content-Type: text/markdown` (httpx2 doesn't auto-set it for `content=str`); `If-Match` / `If-None-Match: *` both wired, `if_match` wins if both are passed. 16 Layer-3 tests under `httpx.MockTransport` (no real SB), all green in 0.07s; T1 smoke unbroken. Write-envelope fog (T8) deliberately not resolved here — `write_page` only sends the two headers the design doc requires, the real-write attempt is what decides the rest.
- [T4. `verifier.py` + `server.py` (the MCP tools)](#t4-verifierpy--serverpy-the-mcp-tools) (commit `845b9`): inbound half + tool wiring — `StaticTokenVerifier` (constant-time `hmac.compare_digest`, returns `AccessToken` with scopes `['notes:read', 'notes:write']`); `build_mcp(sb_client, *, token, resource_url)` factory returning an `MCPServer` with `AuthSettings(issuer_url=resource_url, resource_server_url=resource_url)` (no separate authz server, v1 honest about that); three `@mcp.tool()` handlers (`read_page`, `write_page` with `if_match`, `list_pages` with `prefix`); one `@mcp.resource()` template `silverbullet://page/{name}` returning `text/markdown`. SB exceptions map to MCP exceptions per the § Tools status-code table: 404 → `ToolError('page not found: {name}')` in tools, `ResourceNotFoundError` (SEP-2164, -32602) in the resource template; 412 → `ToolError('precondition failed; X-Client-Id seen')`; 413 → `ToolError('body too large: limit is 4 MiB')`; 5xx → `ToolError('silverbullet error: {status}')`; timeout → `ToolError('silverbullet request timed out')`. 15 Layer-1 tests under `Client(mcp)` in-memory + `httpx.MockTransport`, all green in 1.08s; 31 tests total green; T1 smoke unbroken; `nix flake check` green. v2.x carry-forwards noted in code: `MCPServer` (not `FastMCP`); `AuthSettings` requires both URLs (so we point both at the resource URL); `ToolError` from a tool handler is wire-level `is_error=True`, from a resource handler becomes `UnexpectedResourceError` → `MCPError` (so the resource template uses `ResourceError` shapes for the same message text).
- [T5. HTTP integration tests (auth + discovery doc)](#t5-http-integration-tests-auth--discovery-doc) (commit `53ae8`): 5 Layer-2 tests in `tests/test_http_auth.py` exercising the bridge as a real ASGI app via `httpx.ASGITransport(app=streamable_http_app(host="bridge.test"))` + `app.router.lifespan_context(app)` (drives the SDK's `StreamableHTTPSessionManager.run()` since `ASGITransport` doesn't speak Starlette's lifespan protocol). Covers: POST `/mcp` no-token → 401 + `WWW-Authenticate: Bearer error="invalid_token", error_description="Authentication required", resource_metadata="http://bridge.test/.well-known/oauth-protected-resource/mcp"`; POST `/mcp` wrong-token → identical shape (no header-vs-token probe leak); GET `/.well-known/oauth-protected-resource/mcp` (no auth) → 200 + RFC 9728 doc with `resource=<resource_url>`, `authorization_servers=[<resource_url>]` (v1 has no separate authz server), `bearer_methods_supported=["header"]`, `scopes_supported` omitted (`AuthSettings.required_scopes=None`, stripped by `PydanticJSONResponse.render`); POST `/mcp` with auth but `Accept: text/plain` → 406 ("Not Acceptable: Client must accept both application/json and text/event-stream"); end-to-end `streamable_http_client` + `ClientSession` initialize → list_tools (`['list_pages', 'read_page', 'write_page']`) → `call_tool read_page` roundtrip against a mocked SB. ASGI transport vs the ticket's literal "uvicorn on a free port": same wire path (every middleware, header, status code, body shape) minus the TCP stack — fast, deterministic, no port flake. The real-port path is exercised at T7 against the live SilverBullet. 5 new tests in 0.94s; 36 tests total green; `nix flake check` green.
- [T6. Operator smoke run + README](#t6-operator-smoke-run--readme): `main.py` boots `SBClient` + `build_mcp` + `run_streamable_http_async`; `flake.nix` exposes `apps.mcp-silverbullet`; README documents boot order. Live smoke against SB `127.0.0.1:63000` (no SB auth): write_page + read_page roundtrip OK; list_pages → ToolError `silverbullet error: 307` because `GET /.fs` redirects to `/` on this SB. `mcp` CLI extra not installed; walkthrough used `ClientSession` + `streamable_http_client` instead of `mcp dev`. *(T10 fix: `sb_client.list_pages` now sends `X-Sync-Mode: 1` so this no longer 307s on real SB.)*
- [T7. Live-SB end-to-end test (env-gated)](#t7-live-sb-end-to-end-test-env-gated) (commit `7025d`): `tests/test_e2e_live_sb.py` skips unless both `MCP_SILVERBULLET_LIVE_SB_URL` and `MCP_SILVERBULLET_LIVE_SB_TOKEN` are in env (empty token allowed). With them set, boots `serve()` on a free port, Streamable HTTP write/read roundtrip of `e2e-mcp-silverbullet-marker.md`, `If-Match: *` update succeeds, stale etag does **not** 412 (this SB ignores precondition headers), `list_pages` still ToolError 307. Marker deleted in `finally`. Unset env: skip. 40 passed + 1 skipped without live env. File was untracked at the time T7 was marked resolved on the map; commit `7025d` retroactively stages the file with no changes. *(T10 promoted the `X-Sync-Mode` fix and updated this test to assert the marker appears in the structured payload instead of treating the 307 as expected behavior.)*
- [T8. Write-envelope fog (X-* headers on PUT)](#t8-write-envelope-fog-x--headers-on-put) (commit `9275c`): full design-doc envelope — `write_page` sends every X-* header `docs/design.md` § SilverBullet client contract calls out for PUT, on top of the static `X-Source: external` + `X-Permission: rw` + `Content-Type: text/markdown`. New per-call fields: `X-Created = X-Last-Modified = int(time.time_ns() / 1_000_000)` (epoch ms, the unit SB's `header_i64` parses); `X-Content-Length = len(content.encode("utf-8"))` (UTF-8 byte count, matching SB's `meta.size`). Static fields stay in `_WRITE_HEADERS`; new `_epoch_ms()` helper isolates the timestamp computation. Operator chose this path on the basis that the design doc calls for the full envelope and SB's PUT handler reads (but mostly ignores) these from the request — `server-common/src/space/disk.rs::write_file` honors `meta.last_modified > 0` (stamps file mtime) but ignores `meta.created / meta.size / meta.perm`. Live PUT against the dev-box SB on `127.0.0.1:63000` succeeded with the new envelope; response carried `X-Last-Modified` matching our value (file mtime honored) and `X-Created` a few ms later (file btime, since the disk impl ignores request `created`). Bridge doesn't observe either side. 41 passed + 1 skipped.
- [T10. Journal-tools config gate (foundation)](#t10-journal-tools-config-gate-foundation) (commit `46d81`): new module `src/mcp_silverbullet/journal.py` (gate + four skeleton tools); `Settings.journal` resolved by `load_settings`; `build_mcp(..., journal=...)` calls `register_journal_tools` only when `JournalConfig.enabled`. Three-gate check (truthy opt-in / non-empty path / readable) → INFO log on open, WARN on requested-but-unusable, silent on off. Skeleton tools raise `ToolError("journal tool not implemented yet; landing in T11/T12")` so a stray call surfaces loudly. Drive-by: `sb_client.list_pages` now sends `X-Sync-Mode: 1` so SB 2.x's `handle_fs_list` returns JSON instead of 307-redirecting to the SPA (prior map's T3 mock-only coverage missed it; T6 smoke parked it as "effectively moot" once the journal surface replaced the original search tool). New `tests/test_journal_gate.py` (11 cases). 54 passed (with live env) / 53 passed + 1 skipped (without); `nix flake check` green.
- [T11. Read tools (histogram / tag_summary / recent_pages)](#t11-read-tools-histogram--tag_summary--recent_pages): three of the four T10 skeletons replaced with real implementations in `src/mcp_silverbullet/journal.py`. New helpers: `_validate_prefix` (rejects `..` / leading `/` with a `ToolError`), `_iter_md` (`rglob("*.md")` filtered to skip hidden directory segments), `_bucket_key` (basename regex `^\d{4}-\d{2}-\d{2}` → `YYYY-MM`, else UTC mtime), `_parse_tags` (hand-rolled frontmatter scanner for `tags: scalar` OR `tags:\n  - item\n  - item` — no PyYAML dep), `_unquote`, `_mtime_iso` (UTC ISO-8601), `PageRef` dataclass. New tests `tests/test_journal_read.py` (19 cases); inverted the T10 skeleton-error tests in `tests/test_journal_gate.py` so only `pages_touching_topic` (T12) still raises the skeleton error. Three SDK-shape carry-forwards worth flagging: (1) `dict[str, X]` return types emit the dict directly via a `RootModel` (no `{"result": …}` wrap); (2) `list[X]` returns *are* wrapped in `{"result": …}`; (3) `ToolError` raised from a tool handler is wire-level `is_error=True` with the message prefixed `"Error executing tool <name>: "`. Live smoke against `/var/lib/silverbullet/`: histogram returns the real distribution (`2023-10: 18`, `2024-09: 7`, …), `tag_summary` top tags are `daily: 75`, `quick: 36`, `daily-journal: 33`, `contact: 20`, …, `recent_pages(limit=5, prefix="Daily")` returns the five newest `Daily/*.md`. 72 passed + 1 skip (T7 env-gated); `nix flake check` green.
- [T12. `pages_touching_topic` (search by name + content)](#t12-pages_touching_topic-search-by-name--content): last T10 skeleton replaced with a real name+content substring search in `src/mcp_silverbullet/journal.py`. Optional `rg --json` acceleration (when on PATH; `_RG_BIN` cache, `_RG_TIMEOUT_S` 30s, `--no-config --no-messages -i`); falls back to pure-Python substring scan on `rg` failure / timeout / absence. New helpers: `_rg_available`, `_rg_content_matches`, `_safe_read_body`, `_content_snippet`, `_body_excerpt`, `_normalize_query`. Name match is against the **relative path** (the ticket said "basename" but the done-when example `query="DAILY"` finding `Daily/*.md` requires the relative path; this matches the `prefix` filter's behavior for consistency). Snippet shape: line containing the match returned whole if it fits in 80 chars, else windowed to 80 chars centered on the match with `…` ellipses. Empty / whitespace-only queries raise `ToolError`. Empty space returns `[]`. Results sorted by name-asc. New tests `tests/test_journal_search.py` (25 cases): input validation (empty / whitespace / `..` / `/` prefix), name-only / content-only / both match kinds, snippet shaping (short-line verbatim, long-line windowed, correct-line selection, frontmatter stripping in body excerpts), prefix filtering, hidden-dir skip, literal-substring semantics (no regex metachar activation), whitespace-collapsing query, `rg`-available path, `rg` timeout fallback, `rg` non-zero-exit fallback, list-payload wrapping, mid-iteration file disappearance. Live smoke against `/var/lib/silverbullet/` with `MCP_SILVERBULLET_JOURNAL_TOOLS=1`: 130 hits on `query="DAILY"` covering all three match kinds (pure name, pure content, both), Python path ≈ rg path ≈ 18ms each. `flake.nix` dev shell now carries `pkgs.ripgrep` so the rg path runs from `nix develop`; runtime still doesn't depend on it. 96 passed + 1 skip (T7 env-gated).

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
> **Assignee**: claude (claimed 2026-08-27, resolved same day)
> **Status**: ✅ resolved (commit `845b9`)
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
> **Resolution**: see commit `845b9`. Three v2.x SDK carry-forwards worth flagging for future server work: (1) `MCPServer` is the v2.x name (not `FastMCP` — that was the v1-era name); (2) `AuthSettings` requires both `issuer_url` and `resource_server_url` to enable the bearer-auth middleware, so v1 points both at `resource_url` (no separate authz server per T2 of the prior map); (3) `ToolError` raised from a *tool* handler becomes a wire-level `is_error=True` content block (prefixed `"Error executing tool <name>: "`), while `ToolError` raised from a *resource* handler becomes `UnexpectedResourceError` → `MCPError` (a JSON-RPC protocol error) — so the resource template raises `ResourceNotFoundError` (SEP-2164, `-32602`) for 404 and `ResourceError` (`-32603`) for everything else, with the same message text. 15 Layer-1 tests under `Client(mcp)` in-memory + `httpx.MockTransport` (no real SB), all green in 1.08s; 31 tests total green; T1 smoke unbroken; `nix flake check` green. The T6 ticket now has the missing boot-piece (the `MCPServer` factory) — T6 will wire it into `main.py` and drive `nix run .#mcp-silverbullet` against the live SB.

---

### T5. HTTP integration tests (auth + discovery doc)

> **Labels**: `wayfinder:task`
> **Type**: AFK
> **Assignee**: claude (claimed 2026-08-27, resolved same day)
> **Status**: ✅ resolved (commit `53ae8`)
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
> **Resolution**: see commit `53ae8`. Five Layer-2 tests in
> `tests/test_http_auth.py` (~290 lines, 0.94s). The bridge runs as a
> real ASGI app on a real wire path via `httpx.ASGITransport(app=...)
> + app.router.lifespan_context(app)`; the SDK's auto-enabled
> DNS-rebinding protection is bypassed by passing
> `host="bridge.test"` to `streamable_http_app` (anything other than
> 127.0.0.1/localhost/::1 turns the protection off; the loopback
> case is the production default and T6 will thread
> `MCP_SILVERBULLET_ALLOWED_HOSTS` through it). ASGITransport does
> not speak Starlette's lifespan protocol, so we drive
> `app.router.lifespan_context(app)` manually to enter the SDK's
> `StreamableHTTPSessionManager.run()` before any request — without
> that, every `/mcp` POST crashes at
> `"Task group is not initialized. Make sure to use run()."` Three
> SDK shape facts the resolution locked down for future HTTP work:
> (1) `PydanticJSONResponse.render` calls
> `model_dump_json(exclude_none=True)`, so any `None` field on
> `ProtectedResourceMetadata` is stripped from the response body —
> `scopes_supported` is omitted because `AuthSettings.required_scopes`
> is unset; if we want scopes advertised, T6 wires
> `AuthSettings(required_scopes=[...])` or a later map adds it.
> (2) The 401 path returns JSON body
> `{"error":"invalid_token","error_description":"Authentication required"}`
> (the SDK shape, not RFC 6750 §3's `error="invalid_token", error_description="..."` parameters — those go in the `WWW-Authenticate`
> header); a future Grok connector that parses the body expects this
> shape. (3) `Accept: text/plain` (with valid auth) returns 406 only
> because the auth middleware sits in front of the streamable handler;
> a wrong Accept without auth would still 401 — the test exercises
> the inner branch deliberately.

---

### T6. Operator smoke run + README

> **Labels**: `wayfinder:task`
> **Type**: AFK (agent runs it locally and reports; no human in the
> loop)
> **Assignee**: pi (claimed 2026-08-27, resolved same day)
> **Status**: ✅ resolved
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
> **Resolution**: Boot path in `src/mcp_silverbullet/main.py`
> (`load_settings` + `serve`). Env contract: required
> `MCP_SILVERBULLET_TOKEN`; `MCP_SILVERBULLET_SB_URL` default
> `http://127.0.0.1:3000`; `MCP_SILVERBULLET_SB_TOKEN` defaults to
> the inbound token, empty string omits `Authorization` (this
> dev-box SB has no auth). `MCP_SILVERBULLET_RESOURCE_URL` defaults
> to `http://{host}:{port}/mcp`. `MCP_SILVERBULLET_ALLOWED_HOSTS`
> comma-list becomes `TransportSecuritySettings.allowed_hosts`.
> `flake.nix` `apps.default` / `apps.mcp-silverbullet` point at the
> venv console script. README boot order + env table.
> Smoke (venv, port 18000, SB `127.0.0.1:63000`, empty SB token):
> initialize → tools `[list_pages, read_page, write_page]` →
> `write_page e2e-mcp-silverbullet-t6.md` OK → `read_page` returned
> `hello from T6 smoke` → `list_pages prefix=e2e-mcp` ToolError
> `silverbullet error: 307` (`GET /.fs` 307 to `/`; `GET /.fs/{name}`
> and `PUT`/`DELETE` work). Page deleted via `DELETE /.fs/...` (200).
> `mcp` CLI extra (`mcp[cli]`) is not in the lockfile; walkthrough
> used SDK `ClientSession` over Streamable HTTP, same wire as
> `mcp dev`. 4 new tests in `tests/test_main_settings.py`; 40 tests
> green. T7 should skip or soften `list_pages` until the list
> endpoint is mapped on this SB.

---

### T7. Live-SB end-to-end test (env-gated)

> **Labels**: `wayfinder:task`
> **Type**: AFK
> **Assignee**: pi (claimed 2026-08-27, resolved same day)
> **Status**: ✅ resolved
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
> **Resolution**: Env-gated test as specified. Live run against
> `http://127.0.0.1:63000` with empty SB token: write + read of the
> marker page matched; `If-Match: *` update 200; stale `If-Match`
> **also 200** — this SilverBullet does not implement 412 on PUT
> preconditions (probed with curl: If-Match, If-None-Match, missing
> page all 200). `list_pages` still `silverbullet error: 307`.
> Test records those SB facts instead of failing the suite. Cleanup
> `DELETE /.fs/{marker}` in `finally`. Without env vars, pytest skips
> with the message in the test. PUT responses also returned
> `X-Content-Length`, `X-Created`, `X-Last-Modified`, `X-Permission` —
> the response-side mirror of the request-side envelope T8 settled on.

---

### T8. Write-envelope fog (X-* headers on PUT)

> **Labels**: `wayfinder:grilling`
> **Type**: HITL (operator decided which headers survive a real write)
> **Assignee**: pi (claimed 2026-08-27, resolved same day)
> **Status**: ✅ resolved (commit `9275c`)
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
> **Resolution**: Operator chose the full design-doc envelope on a
> grilling round (T8 was the only HITL ticket on this map). Reading
> `server/src/handlers/fs.rs::handle_fs_put` and
> `server-common/src/space/disk.rs::write_file` first locked the
> facts: SB's PUT reads `Content-Type` / `X-Created` /
> `X-Last-Modified` / `X-Content-Length` / `X-Permission` from the
> request, threads them as `meta: Option<&FileMeta>` into
> `write_file`, and the disk impl only honors `last_modified > 0`
> (stamps file mtime). With those facts grounded, the operator's
> "Full design-doc envelope" answer settled the policy: send all
> five headers with sensible values (`X-Created = X-Last-Modified =
> int(time.time_ns() / 1_000_000)`; `X-Content-Length = len(content.
> encode("utf-8"))`). Live PUT against `127.0.0.1:63000` returned
> 200 with `X-Last-Modified` echoed and `X-Created` slightly
> adjusted to the file's actual btime — confirming the disk impl's
> `last_modified`-honors / `created`-ignores split. New test
> `test_write_page_x_content_length_matches_utf8_byte_count` guards
> the codepoint-vs-byte bug class. 41 tests pass + 1 skipped.

---

### T9. **Superseded by T10–T13.**

> **Labels**: `wayfinder:grilling` (replaced)
> **Status**: closed as superseded; replaced by T10–T13 below.
> **What happened**: After T9 settled the *shape* of search
> (name + content, case-insensitive substring, `{name, match,
> snippet}` hits) but stalled on the *implementation path* — the
> `/.fs` list endpoint is broken on this SB build and Space Lua was
> ruled out by T4 of the prior map — the operator redirected:
> *“assume the MCP is running on the same machine as the
> SilverBullet folder and has access to it, gate that behind a
> configurable condition.”* The new direction is a *direct-FS
> journal surface* (T10–T13) instead of a single search tool. The
> name+content hit shape T9 settled carries forward into T12's
> `pages_touching_topic`.

---

### T10. Journal-tools config gate (foundation)

> **Labels**: `wayfinder:task`
> **Type**: AFK
> **Assignee**: pi (claimed 2026-08-29, resolved same day)
> **Status**: ✅ resolved (commit `46d81`)
> **Question**: How does the bridge decide whether to register the
> journal tools?
> **Context**: The journal surface (T11, T12) reads the SB space
> directory directly from disk — `/var/lib/silverbullet/` on this
> dev box. That requires two new env vars: `MCP_SILVERBULLET_SPACE_PATH`
> (absolute path to the space directory; required for journal
> tools) and `MCP_SILVERBULLET_JOURNAL_TOOLS` (a boolean flag —
> truthy means register the journal tools at boot; falsy means
> the bridge boots without them).
>
> Three-gate check at bridge boot: (1) `space_path` is set and
> non-empty; (2) `os.access(space_path, os.R_OK)` returns True;
> (3) `journal_tools` is truthy. If any fails, the bridge logs a
> one-line `INFO`/`WARN` and skips journal-tool registration.
> All existing tools (`read_page`, `write_page`, `list_pages`,
> `silverbullet://page/{name}`) still work — the journal tools
> are strictly additive. **Existing T1–T8 tests must continue to
> pass with the new env vars unset.**
>
> **Files when resolved**: `src/mcp_silverbullet/main.py` (load
> new vars), `src/mcp_silverbullet/server.py` (call into the
> journal module from `build_mcp`), new
> `src/mcp_silverbullet/journal.py` (the gate + skeleton).
> **Tests when resolved**: Layer-1 boot tests for both modes
> (gate on / gate off) — assert journal tools are present or
> absent from `await client.list_tools()`. Reuse the
> `httpx.MockTransport` pattern from existing tests; no live FS
> needed.
> **Unblocks**: T11, T12.
> **Done when**: the bridge boots cleanly with the journal tools
> *off* (existing tests pass), the bridge boots cleanly with the
> tools *on* and `space_path` readable (skeleton journal tools
> registered), the bridge boots cleanly with the tools *on* but
> `space_path` not readable (warning + tools not registered).
> **Resolution**: new module `src/mcp_silverbullet/journal.py`
> exposes `resolve_journal_config(environ) -> JournalConfig(enabled,
> space_path)` + `register_journal_tools(mcp, config)`. Three-gate
> check at resolve time: (1) `MCP_SILVERBULLET_JOURNAL_TOOLS`
> truthy (`1`/`true`/`yes`/`on`); (2)
> `MCP_SILVERBULLET_SPACE_PATH` non-empty; (3) `os.access(path, os.R_OK)`.
> Failure of any emits a single INFO/WARN line and the gate closes.
> `Settings.journal` is resolved by `load_settings` and threaded
> into `build_mcp(..., journal=...)`; `build_mcp` calls
> `register_journal_tools` only when the gate is on. Four skeleton
> tools (`journal_histogram`, `tag_summary`, `recent_pages`,
> `pages_touching_topic`) raise `ToolError("journal tool not
> implemented yet; landing in T11/T12")` on call; T11/T12 replace
> the bodies. `logging.basicConfig(level=INFO, ...)` at the top of
> `serve` so the gate-open log reaches the operator's terminal
> (without it the root logger discards INFO and the line is
> invisible — discovered by the live smoke, not the unit tests).
> New tests `tests/test_journal_gate.py` (11 cases) cover every
> gate branch (off, on-but-unreadable, on), `build_mcp` integration
> per branch, skeleton tool error shape, and the disabled-config
> no-op. Layer-1 + Layer-2 + T7 live e2e all green (54 pass + 0
> skip with live SB env, 53 pass + 1 skip without); `nix flake
> check` green.
> **Drive-by bug fix**: `sb_client.list_pages` now sends
> `X-Sync-Mode: 1` on `GET /.fs`. SB's
> `server/src/handlers/fs.rs::handle_fs_list` only returns JSON
> when this header is set; without it, SB 307-redirects to the SPA
> UI — the bridge saw a 307 and surfaced
> `ToolError('silverbullet error: 307')` against every real SB.
> T3 mock-only coverage never caught it (respx doesn't run the
> handler logic); T6 recorded the 307 in the smoke run and parked
> the fix in the map's fog as "effectively moot" once the journal
> surface replaced the original search tool. Promoting the fix in
> T10 because the existing `/.fs`-backed `list_pages` tool still
> ships in v1 and deserved the same fix; new test
> `test_list_pages_sends_x_sync_mode` guards the header. T7 live
> e2e now asserts the marker is in the structured payload instead
> of treating the 307 as expected behavior.

---

### T11. Read tools (histogram / tag_summary / recent_pages)

> **Labels**: `wayfinder:task`
> **Type**: AFK
> **Assignee**: claude (claimed 2026-08-29, resolved same day)
> **Status**: ✅ resolved
> **Question**: What three pure-Python read tools does the
> journal surface expose?
> **Context**: With T10's gate in place and the four skeleton
> tools registered, T11 replaces three of them with real
> implementations that are *purely* filesystem reads — no
> subprocess, no `rg`, no external deps. All three accept an
> optional `prefix: str = ""` parameter (validated: no `..`, no
> leading `/`, treated as a path-segment substring against file
> names; if non-empty, restricts the inventory to files whose
> relative path contains the segment).
>
> - **`journal_histogram(prefix: str = "") -> dict[str, int]`** —
>   walk `*.md` under `space_path` (filtered by `prefix`),
>   bucket by `YYYY-MM` extracted from the *filename* if it
>   matches `^\d{4}-\d{2}-\d{2}` (the SB daily-journal naming
>   convention), else from the file's mtime. Returns
>   `{"2025-12": 22, "2026-01": 27, ...}` sorted by key.
> - **`tag_summary(prefix: str = "") -> dict[str, int]`** —
>   walk `*.md` files, parse YAML frontmatter (`---\n...\n---\n`),
>   extract `tags:` (single string or list of strings; case
>   preserved). Returns `{"meta": 50, "daily": 111, ...}`
>   sorted by count desc.
> - **`recent_pages(limit: int = 10, prefix: str = "") ->
>   list[PageRef]`** — files sorted by mtime desc, returns
>   `{name, mtime_iso, size_bytes}[]` truncated to `limit`.
>   `PageRef` is a frozen dataclass; same shape as `FileMeta`
>   plus `mtime_iso`.
>
> **Files when resolved**: `src/mcp_silverbullet/journal.py`
> (replacing the three T10 skeleton bodies), no other module
> changes. **Tests when resolved**: Layer-1 tests against a
> tmpdir fixture populated with synthetic `*.md` files (one
> with frontmatter, one without; one matching
> `^\d{4}-\d{2}-\d{2}`, one not). Asserts on the three return
> shapes for empty space, prefix-restricted space,
> mixed-content space. Skeleton-error tests in
> `test_journal_gate.py` get dropped (or inverted: assert
> real behavior, not placeholder errors).
> **Blocks on**: T10.
> **Unblocks**: T13.
> **Resolution**: replaced the three T10 skeleton bodies with
> real implementations in `src/mcp_silverbullet/journal.py`.
> New helpers: `_validate_prefix` (rejects `..` and leading
> `/` with a `ToolError` before any FS call), `_iter_md`
> (`Path.rglob("*.md")` filtered to skip hidden directory
> segments like `.git` / `.cache` / `.ssh`), `_bucket_key`
> (basename regex `^\d{4}-\d{2}-\d{2}` → `YYYY-MM`; falls
> back to UTC mtime), `_parse_tags` (hand-rolled frontmatter
> scanner for `tags: scalar` OR `tags:\n  - item\n  - item`;
> no PyYAML — the standing-preferences dep policy is
> off-the-shelf only, and SB's frontmatter shape is
> bounded), `_unquote` (strips a matching pair of `'`/`"`
> from a tag value), `_mtime_iso` (UTC ISO-8601), `PageRef`
> dataclass. Frontmatter parser returns `[]` for malformed
> frontmatter (no closing fence) rather than raising — the
> tool's job is to count tags, and we'd rather under-count
> than refuse to return.
>
> Three SDK-shape facts the resolution locked down (future
> journal work hits them again): (1) `dict[str, X]` return
> types go through a Pydantic `RootModel` and the
> `structured_content` payload is the dict itself, NOT wrapped
> in `{"result": …}`; (2) `list[X]` return types ARE wrapped
> in `{"result": …}` — so `journal_histogram` / `tag_summary`
> tests assert on the bare dict, while `recent_pages` asserts
> on `{"result": […]}`; (3) the SDK prepends `"Error executing
> tool <name>: "` to `ToolError` text raised from a tool
> handler, so `is_error=True` text includes that prefix.
>
> New tests: `tests/test_journal_read.py` (19 cases) covers
> empty space, daily-filename bucketing, mtime fallback,
> substring prefix (with the documented "Areas/Daily Notes.md
> still matches prefix='Daily'" behavior), hidden-dir skip,
> `..` / `/` prefix rejection (raises `ToolError`), scalar +
> list tag shapes, case preservation, quote stripping,
> malformed frontmatter, mtime-desc ordering, `limit`
> truncation (and `limit=0`), the `name`/`mtime_iso`/
> `size_bytes` payload shape, and `mtime_iso.endswith("+00:00")`.
> Inverted the T10 skeleton-error tests in
> `tests/test_journal_gate.py`: now the three T11 tools return
> empty-collection shapes against an empty `tmp_path`, and
> only `pages_touching_topic` (T12) still raises the skeleton
> error.
>
> Live smoke against `/var/lib/silverbullet/` with
> `MCP_SILVERBULLET_JOURNAL_TOOLS=1`:
> `journal_histogram` returns the real distribution
> (`2023-10: 18`, `2024-09: 7`, …), `tag_summary` top tags
> are `daily: 75`, `quick: 36`, `daily-journal: 33`,
> `contact: 20`, …, `recent_pages(limit=5, prefix="Daily")`
> returns the five newest `Daily/*.md` files, and
> `pages_touching_topic` still raises the T12 skeleton
> error. 72 tests pass + 1 skip (T7 env-gated); `nix flake
> check` green.

---

### T12. `pages_touching_topic` (search by name + content)

> **Labels**: `wayfinder:task`
> **Type**: AFK
> **Assignee**: pi (claimed 2026-08-29, resolved same day)
> **Status**: ✅ resolved
> **Question**: How does the bridge expose name+content search
> without `rg` as a hard dep?
> **Context**: This replaces the T10 skeleton body for the
> fourth journal tool and resolves the original T9
> search-by-name-and-content ask. Two-step search: (1) walk
> `*.md` files under `space_path` (optionally filtered by
> `prefix`); (2) for each file, check the basename (case-
> insensitive substring) AND the body (case-insensitive
> substring) for the query. Returns
> `{name, match: "name"|"content"|"both", snippet: str}[]`,
> where `snippet` is the first ~80-char Markdown-shaped window
> around the content match (or a short body excerpt for
> name-only matches).
>
> Two implementation strategies: (a) `rg` if available on PATH
> (`shutil.which("rg") is not None`), called via subprocess with
> a strict allow-list of flags (`--no-heading --line-number
> --no-config` plus user `query`); (b) Python `pathlib` + `re`
> fallback otherwise. Either path returns the same shape.
>
> **Files when resolved**: `src/mcp_silverbullet/journal.py`
> (replacing the T10 skeleton body for `pages_touching_topic`).
> New dep: none on Python side (`rg` is optional). The flake
> env should *optionally* include `ripgrep` so the dev shell
> has it; runtime doesn't depend on it.
> **Tests when resolved**: Layer-1 tests with the Python
> fallback path (force `rg` unavailable in fixture). Layer-2
> test against the live `/var/lib/silverbullet/` (env-gated,
> like the T7 live-SB test).
> **Blocks on**: T10.
> **Unblocks**: T13.
> **Done when**: the inverted-style `query="DAILY"` finds every
> `Daily/*.md` (name match) plus any page whose body mentions
> "DAILY"; the empty-result case returns `[]`; the
> `tool … not implemented yet` skeleton error is gone for
> this tool.
> **Resolution**: replaced the T10 skeleton body with a real
> name+content substring search in `src/mcp_silverbullet/journal.py`.
> The bridge reads every file's body for the snippet anyway, so the
> ``rg --json`` acceleration only saves body reads for files that
> have neither a name nor a content match — useful when the space
> is large, immaterial when it's not (the operator's 130-page
> ``Daily``-query finished in 18ms on both paths).
>
> Name-match interpretation: the ticket said "basename" but the
> done-when example (``query="DAILY"`` finding every ``Daily/*.md``)
> requires matching the **relative path** (``Daily/2026-01-05.md``
> does not contain "DAILY" in its basename). The relative-path
> reading is also consistent with how the ``prefix`` filter works
> (``prefix="Daily"`` already matches ``Areas/Daily Notes.md`` via
> its relative path). Implementation matches the relative path
> ``name`` from :func:`_iter_md`; if the operator wanted strict
> basename matching, that's a one-line change in
> :func:`_pages_touching_topic` (replace ``q_lower in name.lower()``
> with the equivalent on ``Path(name).name``).
>
> New helpers in :mod:`mcp_silverbullet.journal`:
>
> - :func:`_rg_available` — memoized ``shutil.which("rg")``;
>   monkeypatch ``_RG_BIN`` to force the Python path (the
>   ``force_python_path`` fixture in ``tests/test_journal_search.py``
>   does this).
> - :func:`_rg_content_matches` — runs ``rg --json -i --no-config
>   --no-messages -- <query> <files...>``, parses one JSON record
>   per line, returns ``{name: first_match_line}`` (empty dict when
>   ``rg`` succeeded with no matches, ``None`` on error / timeout
>   so the caller falls through to the Python path). Subprocess
>   timeout ``_RG_TIMEOUT_S`` = 30s; ``rg`` exit codes ``0`` /
>   ``1`` (no matches) treated as success, anything higher is a
>   ``WARN``-logged fallback trigger.
> - :func:`_safe_read_body` — ``path.read_text("utf-8")`` with
>   ``OSError`` / ``UnicodeDecodeError`` swallowed to ``None``.
> - :func:`_content_snippet` — line containing the match returned
>   whole if ``len(line) <= _SNIPPET_MAX_LEN`` (80), else
>   windowed to 80 chars centered on the match with ``…``
>   ellipses. Lines are stripped; multi-line bodies pick the line
>   with the first occurrence.
> - :func:`_body_excerpt` — first 80 chars of body with YAML
>   frontmatter stripped (same shape as :func:`_parse_tags`); used
>   for name-only snippet.
> - :func:`_normalize_query` — strip + ``" ".join(query.split())``
>   to collapse internal whitespace (including newlines); empty
>   result raises :exc:`ToolError`.
>
> Wire shape: ``list[dict[str, str]]`` returns are wrapped in
> ``{"result": [...]}`` per the T11 carry-forward; each row is
> ``{name, match, snippet}`` with ``match`` ∈ ``{"name",
> "content", "both"}``. Sort key is name-asc so the result is
> deterministic regardless of the underlying walk order.
>
> Tests: ``tests/test_journal_search.py`` (25 cases). Covers
> input validation (empty / whitespace-only / ``..`` / ``/``
> prefix), empty space, no-match space, name-only / content-only
> / both match kinds, snippet shaping (short-line verbatim,
> long-line windowed, correct-line selection across multiple
> lines, frontmatter stripping in body excerpts), name-match
> against relative path (the operator's daily-journal use case),
> multi-result ordering (name-asc), prefix filtering,
> hidden-dir skip (carried from :func:`_iter_md`), literal-
> substring semantics (regex metachars like ``.*`` don't activate
> as regex), whitespace-collapsing query (``"the\tbridge\nis"``
> matches ``"the bridge is"``), the ``rg --json`` path on a real
> ``rg`` install (skipped when absent), the ``rg`` timeout
> fallback path, the ``rg`` non-zero-exit fallback path,
> list-payload wrapping (T11 carry-forward), and a mid-iteration
> file-disappearance tolerance (``_safe_read_body`` returns
> ``None`` and the file is skipped).
>
> Drive-by: ``flake.nix`` ``devShells.${system}.default`` now
> includes ``pkgs.ripgrep`` so the rg path runs from ``nix
> develop``; runtime still doesn't depend on it (the bridge's
> Python code probes ``shutil.which("rg")`` at boot and
> degrades cleanly). ``README.md`` journal surface list
> expanded with the ``pages_touching_topic`` shape (``{name,
> match, snippet}[]``) and the rg-acceleration note.
>
> Live smoke against ``/var/lib/silverbullet/`` with
> ``MCP_SILVERBULLET_JOURNAL_TOOLS=1``: ``query="DAILY"``
> returns 130 hits — pure name matches (``Daily Affirmation.md``,
> ``Daily/2023-10-05.md``, …), pure content matches
> (``Areas/Open Loops.md`` body mentions "daily journaling"),
> and ``both`` matches (``Daily/2023-10-05.md`` whose body
> says "- daily-journal"). Both Python and ``rg`` paths return
> identical results in ~18ms. The ``pages_touching_topic`` T12
> skeleton error is gone for good.

---

### T13. Live-space test for the journal tools

> **Labels**: `wayfinder:task`
> **Type**: AFK
> **Status**: open
> **Blocks on**: T11, T12.
> **Question**: How do we exercise the journal tools against
> the live space at `/var/lib/silverbullet/`?
> **Context**: T7's env-gated live-SB test exercises the
> `/.fs`-backed tools. The journal tools don't go through SB
> at all — they read the FS directly. New test
> `tests/test_e2e_live_journal.py` skips unless
> `MCP_SILVERBULLET_LIVE_SPACE_PATH` is set; with it set, the
> test reads `/var/lib/silverbullet/Daily/` (or whatever the
> env var points at) and asserts `(a)` `journal_histogram()`
> returns a non-empty dict with at least one entry newer than
> 2023-10; `(b)` `tag_summary()` includes `"daily"` as a key;
> `(c)` `recent_pages(limit=5)` returns 5 entries from the
> Daily subdirectory. The test must clean up nothing (read-only)
> and must skip cleanly when the env var is unset.
>
> **Files when resolved**: `tests/test_e2e_live_journal.py`.
> **Done when**: with `MCP_SILVERBULLET_LIVE_SPACE_PATH` set to
> this dev box's space, the three assertions pass; with the
> var unset, the test skips with a clear message. Existing
> Layer-1 + Layer-2 + T7 e2e tests still pass.

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
- Whether the discovery doc should advertise `scopes_supported`
  (`["notes:read", "notes:write"]`, matching what `StaticTokenVerifier`
  grants). T5 confirms the field is currently omitted because
  `AuthSettings.required_scopes` is unset; one-line change to
  publish. Specifiable when a Grok-side client actually parses it.
- `json_response=True` mode for the streamable handler — collapses
  SSE to plain JSON responses, saves a streaming round-trip when the
  client doesn't need event-stream multiplexing. v1 keeps the SSE
  default; specifiable if a measured client needs the JSON path.
- Promoting `TransportSecuritySettings` configuration from the
  loopback auto-default to an explicit operator-tunable shape
  (allowed_hosts / allowed_origins lists) for tunnel-fronted
  deployments. T6 wired `MCP_SILVERBULLET_ALLOWED_HOSTS`; remaining
  fog is allowed_origins / disabling DNS-rebinding entirely.
- `GET /.fs` (no path) on the live SilverBullet (`127.0.0.1:63000`)
  returns the JSON list when `X-Sync-Mode` is set (T10 fix); without
  it SB 307-redirects to the SPA UI. The JSON payload on this SB
  returns `created` / `lastModified` / `contentType` / `size` /
  `perm` per file but **no `etag`** field (only the JSON-list path
  lacks it; `GET /.fs/{name}` returns a proper `ETag` header). The
  bridge's `list_pages` therefore surfaces `etag=None` for every
  page even when the page has a current ETag. Consequence:
  `write_page(..., if_match=<etag>)` has no round-trip path through
  `list_pages` — a tool consumer cannot enumerate pages and then
  issue a conditional update without first calling `read_page` to
  get the ETag. Worth a follow-up ticket (SB-side: emit `etag` in
  the list payload; or bridge-side: fall back to `read_page` for
  ETag lookup). Not blocking v1 (operators can call `read_page` for
  the etag when they need one), but the API is half-broken until
  it's resolved.

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

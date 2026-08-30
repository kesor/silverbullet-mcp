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

## Notes

- **Domain**: protocol bridge between MCP hosts (Grok on the web) and an
  existing HTTP API (SilverBullet `/.fs`, `/.auth`).
- **Skills every session should consult**: `mattpocock/skills@grilling`,
  `mattpocock/skills@domain-modeling`, `modern-web-guidance`, `fullstack-dev`,
  `security-and-hardening`, `incremental-implementation`.
- **Standing preferences for this effort**:
  - **Off-the-shelf libraries only** — prefer `@modelcontextprotocol/server`,
    `hono`, `zod`, nixpkgs over hand-rolled parsers/transports/builders.
  - **One `flake.nix`** owns dependencies for both the runtime and the dev
    shell. No separate `package.json` lockfiles outside what the flake pins.
  - **Side-car process**, not embedded — locked at charter time.
  - **Streamable HTTP transport** is the working default unless T1 says
    otherwise. Already-tested against Cloudflare quick tunnels by xAI docs.
  - The design doc must spell out a path for both `trycloudflare.com` quick
    tunnels (zero-account) and stable named tunnels.
  - **No MCP Apps / no UI resources** — read/write tools only.

## Decisions so far

<!-- index only — one line per closed ticket, link to the ticket's resolution below -->

- **[Side-car integration shape](#t0-integration-shape)**: New repo/dir
  `mcp-silverbullet/`, plain TypeScript on Node 24, separate process from
  SilverBullet. Communicates with SB via its existing `/.fs` and `/.auth` HTTP
  API. (Locked at charter time.)
- **[MCP protocol era](#t-protocol-era)**: 2026-07-28 spec, SDK v2
  (`@modelcontextprotocol/server` 2.0.0). Match nixpkgs unpinned
  (will pin to npm tag at first build).
- **[Cloudflare tunnel model](#t-cf-tunnel-model)**: Already provisioned by
  user; design doc only needs to document boot order and re-connect
  procedure. Out of scope: provisioning automation.

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

> **Labels**: `wayfinder:research`
> **Question**: Streamable HTTP (2025-03-26 onward) or HTTP+SSE (legacy
> 2024-11-05), or both?
> **Files when resolved**: design.md §Transport.
> **Decision-payload expected**: chosen transport name; rejection rationale
> for the alternative; minimum SDK class names.

### T2. Auth model

> **Labels**: `wayfinder:grilling`
> **Question**: Static bearer token in Grok's custom-connector dialog, or
> full OAuth 2.1 with discovery + dynamic-client registration?
> **Files when resolved**: design.md §Auth.
> **Decision-payload expected**: chosen scheme; token-source environment
> variable name; rotation story; what happens if the tunnel rotates URL.

### T3. Stack

> **Labels**: `wayfinder:grilling`
> **Blocked by**: T1.
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

> **Labels**: `wayfinder:research`
> **Blocked by**: T3.
> **Question**: How does the flake consume the chosen SDK?
>   - `nodePackages` derivation with `@modelcontextprotocol/server` pinned?
>   - `pnpm2nix` reading a `pnpm-lock.yaml`?
>   - `dream2nix`?
>   - `buildNpmPackage` with a manually-written lockfile?
> Confirm one approach is smallest and most reproducible.
> **Files when resolved**: design.md §Build, plus a stub `flake.nix` we
> can flesh out post-design.

### T6. Tunnel durability

> **Labels**: `wayfinder:grilling`
> **Question**: `trycloudflare.com` (zero-setup, URL changes each restart)
> vs a named Cloudflare tunnel (free with a domain, stable URL, requires
> account). Which is the design-doc's primary path, and which is the
> fallback? What does the operator do when the URL rotates?
> **Files when resolved**: design.md §Deployment.

### T7. Test surface

> **Labels**: `wayfinder:research`
> **Blocked by**: T3.
> **Question**: How do we exercise the bridge end-to-end without Grok?
> MCP Inspector CLI + a mock SB on `localhost:3010`? Custom vitest fixture?
> Files when resolved: design.md §Testing.

### T8. License

> **Labels**: `wayfinder:grilling`
> **Question**: MIT (matches `@modelcontextprotocol/server`) vs a copyleft
> variant? Repository home (GitHub user, org, none)?
> **Files when resolved**: `LICENSE`, `README.md`.

## Not yet specified

<!-- dim view of what's coming: things we suspect we'll ticket but can't yet phrase precisely -->

- How Grok should discover tool changes mid-session — should the bridge
  support `resources/subscribe` + SSE notifications for live updates, or
  is request/response enough? Depends on T4.
- Whether the design doc should include a SwiftBar / TUI launcher for the
  tunnel + bridge, or leave that to operator scripts.
- A schema for *what* the bridge surfaces from SilverBullet — does it pass
  through Space Lua objects, or only raw markdown? Drag-in of `/.shell` adds
  a rich, dangerous axis.
- Version pinning for `@modelcontextprotocol/server` 2.0 — once nix
  consumption is decided in T5, this becomes a ticket.

## Out of scope

- Provisioning the Cloudflare tunnel itself — already running per user.
- Forking SilverBullet — decision T0 ruled it out.
- Building an MCP Apps (UI) surface — Grok's `mcp-sandbox.grokusercontent.com`
  proxy is a separate spec lane; not needed for read/write tools.
- A general-purpose MCP router or multi-user support — single-user,
  single-instance. Reopen if user base widens.
- Selling or hosting the bridge for other people — local-tunnel-only.

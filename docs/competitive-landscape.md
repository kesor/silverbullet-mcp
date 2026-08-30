# Competitive landscape — `mcp-silverbullet`

**Status:** working draft. Re-run whenever a meaningful new competitor
appears, or every few months to catch drift. The doc feeds the next
build-map (the v1.2 destination is reached; this is research for v1.3).

## Scope and method

I surveyed every SilverBullet MCP project on GitHub I could find (search
`silverbullet mcp`, `silverbullet model context protocol`,
`silverbullet claude`, plus the projects the user pointed me at), read
each project's README and (where it was non-trivial) its source, and
compared them to what `mcp-silverbullet` exposes today. The matrix
below is the result; the rest of the doc is the ranked take on what
fits *our* stated goals.

The matrix uses these abbreviations:

- **Lang.** — implementation language
- **Tr.** — transport (`stdio` / `http` = Streamable HTTP / `both`)
- **Backing** — what API on the SB side the bridge talks to
  - `/.fs` — the SB HTTP file API (what we use)
  - **`fs`** — direct filesystem access to the SB space directory
  - **`/.runtime/lua`** — SB's experimental Runtime API (runs Space Lua
    server-side; needs Chrome / `-runtime-api` Docker variant)
- **Stars** — GitHub stars at time of writing
- **Last push** — most recent commit, from `pushed_at`

## Competitor inventory

| Project | Lang. | Tr. | Backing | Stars | Last push | Notes |
|---|---|---|---|---|---|---|
| `Ahmad-A0/silverbullet-mcp` | TypeScript | http | `/.fs` | 36 | 2026-04 | The community default. Docker Compose, both read & write tools, simple text/JSON envelopes, bare `SB_AUTH_TOKEN`-style bearer. Read README + source of `mcp-tools.js`/`sb-client.js` below. |
| `pepomes/silverbullet-mcp` | Python | http | `/.fs` | 0 | 2026-02 | Small Python port. FastMCP, seven tools (`list/read/create/update/delete/search/get_meta`), `if_match` not implemented. Mostly subsumed by us. |
| `are/mcp-silverbullet` | — | — | — | 0 | 2026-04 | Stub repo — name-claim only (npm `@are/mcp-silverbullet`); the actual code lives in `are/bmad-mcp-silverbullet` below. |
| `are/bmad-mcp-silverbullet` | TypeScript | stdio | `/.runtime/lua` | 0 | 2026-07 | Most ambitious: per-page access modes in plain markdown, read-before-edit freshness invariant, full audit log, no concurrency tokens (relies on freshness). Epic 1 (read) shipped; Epic 2 (writes) ran out of tokens mid-build. The trust model is the intellectual interest of the survey. |
| `bfeller/silverbullet-mcp` | JavaScript | http | `/.fs` | 0 | 2026-08 | Smallest possible bridge. Five tools (`list_notes`, `read_note`, `write_note`, `delete_note`, `search_notes`). Useful as a "what's the minimum?" reference. |
| `xmatthewx/silverbullet-mcp-server` | TypeScript | http | `/.fs` | 0 | 2026-06 | The Claude.ai / OAuth 2.1 specialist. Standalone remote server, collision-safe `lastModified` envelopes, soft-delete trash, structured errors with `remediation`. Uses `expected_last_modified` instead of HTTP `If-Match` (different concurrency-token convention). The most production-hardened competitor. |
| `basedCaesar/silverbullet-mcp-go` | Go | stdio | `/.fs` | 0 | 2026-08 | README-only, "planned" status. Mentioned for completeness; nothing to learn from yet. |
| `lidiaev/me-db` (mcp-server.py) | Python (FastMCP) | http | **fs** | 0 | 2026-08 | Reads/writes the SB space directory directly — completely sidesteps the SB HTTP API. Comes with its own OAuth 2.1 + DCR server, multi-agent append-only model, git-watcher, gzip-friendly `pages_touching_topic` style search. The fs-direct design is its key choice. |
| `emilcechelt/obsidian-multi-agent-memory-stack` | (composite) | — | — | — | — | Not a competitor in the strict sense — a deployment architecture that wires `obsidian-mcp` (Rust, fs-direct, BM25 + embeddings), SilverBullet, CouchDB LiveSync, Syncthing, and a written constitution. Worth reading for the multi-agent governance framing. |
| `lstpsche/obsidian-mcp` (referenced) | Rust | both | **fs** | 26 | active | Not a SilverBullet project, but the obsidian-stack uses it. Its tool set (19 tools, including heading-block-targeted patch, backlinks, BM25 + semantic search, frontmatter get/set/remove, periodic notes, `OBSIDIAN_TOOLS` allow/deny profiles) is the most complete vault-MCP surface in the wild. Treat as a north star for what an "agent-grade" vault MCP looks like. |

The dominant project is `Ahmad-A0/silverbullet-mcp` (36 stars). The
field is otherwise sparse and recent (most projects ≤ 6 months old,
most ≤ 5 stars). That suggests the space is still wide open and our
project has room to lead on quality rather than racing for breadth.

## Feature matrix

The columns are *capabilities*. The rows are projects. ✓ = ships it;
✗ = doesn't ship it; ◐ = ships a partial / gated form. Footnotes
follow the table.

| Capability | ours | Ahmad-A0 | bfeller | pepomes | xmatthewx | are-bmad | lidiaev | obsidian-mcp |
|---|---|---|---|---|---|---|---|---|
| **`read_page`** (single body) | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| **`read_page_many`** (batched, bounded) | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✓ | ✓ |
| **`write_page`** (overwrite) | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ (planned) | ✓ | ✓ |
| **`create_page`** (refuse-overwrite as default) | ✗ | ✗ | ◐ (`create_only` flag) | ✓ | ✓ | ✓ (planned) | ✓ | ✓ |
| **`delete_page`** | ✓ (hard) | ✓ | ✓ | ✓ | ✓ (soft → `_trash/`) | ✓ (planned) | ✓ (auto-prunes empty dirs) | ✓ (`confirm: true`) |
| **`page_exists`** (cheap) | ✓ | ✗ | ✗ | ◐ (`file_exists`) | ✗ | ✗ | ✗ | ✗ |
| **`list_pages`** (with envelope) | ✓ + etag-hydrate opt-in | ◐ (`name + size`) | ◐ (`name`) | ◐ (`name`, sorted text) | ✓ (`path + lastModified`) | ✓ (`ref + lastModified`) | ✓ | ✓ (`vault_list` with metadata + tree) |
| **`append_to_page`** | ✓ + `dry_run` | ✗ | ✗ | ✗ | ✓ | ✓ (planned) | ✓ | ✓ (`note_insert`) |
| **`prepend_to_page`** | ✗ | ✗ | ✗ | ✗ | ✓ (with frontmatter-aware position) | ✗ | ✗ | ✓ (`note_insert position="beginning"`) |
| **`patch_page_lines`** (line-range) | ✓ + `dry_run` | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ |
| **`patch_page_replace`** (literal, uniqueness-guarded) | ✓ + `dry_run` | ✗ | ✗ | ✗ | ✗ | ✗ | ✓ (`replace_in_file` — same uniqueness guard) | ✗ |
| **`move_page`** (rename / move) | ✓ (write-then-delete) | ✗ | ✗ | ✗ | ✓ | ✓ (planned) | ✓ (also moves dirs) | ✓ |
| **`diff_pages`** (read-only diff between two pages) | ✓ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ |
| **`check_task`** / checkbox primitive | ✓ (by wikilink ref) | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✓ (`frontmatter` action) — different shape, see § Code notes |
| **`list_tasks`** (per-page or space-walk) | ✓ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ |
| **Search** (substring / full-text) | ✗ — non-goal | ✓ (`search_notes` substring + snippet) | ✓ | ✓ (rg fallback or grep) | ✓ (`search_pages` substring + snippet) | ✓ (planned) | ✓ (rg, with snippet + line) | ✓ (BM25 + semantic + regex + tag/frontmatter) |
| **Backlinks / wikilinks** | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✓ (`find_backlinks`) | ✓ (`wikilinks` four-way: backlinks/outgoing/broken/orphans) |
| **Periodic-notes helper** | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✓ (`periodic` action=get/create/list) |
| **Frontmatter get/set/remove** | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✓ |
| **Heading-/block-targeted patch** | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✓ (`note_patch` with `target_type` ∈ {heading, block, frontmatter}) |
| **Tag / frontmatter query** | ✓ (via journal `tag_summary`) | ✗ | ✗ | ✗ | ✗ | ✗ | ✓ (`list_files` walks dirs) | ✓ (`search_metadata`) |
| **`vault_info`** / aggregate stats | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✓ |
| **`open_in_obsidian`** (deep-link helper) | ✗ (N/A) | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✓ (Obsidian URI) |
| **Tool allow/deny profiles** | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✓ (`OBSIDIAN_TOOLS=core/read/minimal/!foo,bar`) |
| **Resource template** | ✓ (`silverbullet://page/{name}` JSON envelope) | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ |
| **Concurrency token** | ETag via `If-Match` (HTTP standard) | ✗ | ✗ | ✗ | `expected_last_modified` body field (own convention) | "freshness invariant" (read-before-edit, no token) | ✗ | ✗ |
| **`dry_run` mode on R-M-W tools** | ✓ (4 tools) | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ |
| **Structured error envelope** | partial (HTTP-shaped `ToolError` strings) | ✗ (plain `isError: true` + text) | ✗ (plain `isError: true` + text) | ✗ | ✓ (`{error, status, message, remediation}`) | ✓ (typed `DomainError` with `reason` code) | ✗ | ✓ (`VaultError → MCP ErrorData`) |
| **Audit log** (per-call, on disk) | ✗ (intent is `isError` shape) | ✗ | ✗ | ✗ | ✓ (stderr `[WRITE]` / `[ERROR]` lines) | ✓ (`audit.jsonl` with `{size, sha256}` digest) | ✓ (jsonl logger) | ✗ |
| **Per-page permissions (none/read/append/write)** | ✗ | ✗ | ✗ | ✗ | ✗ | ✓ (in-page `#mcp/config` blocks) | ✗ | ✗ |
| **OAuth 2.1 / DCR** | ✗ — non-goal | ✗ | ✗ | ✗ | ✓ (full spec-compliant) | ✗ | ✓ (full DCR + PKCE + refresh) | ✗ |
| **Bearer-token auth (single shared secret)** | ✓ | ✓ | ✓ | ✓ (path-segment secret) | ✓ (dev bypass) | ✗ (relies on stdio trust) | ✗ (OAuth-only) | ✗ |
| **Sub-path allow-list** | ✗ | ✗ | ✗ | ✗ | planned | ✗ | ✗ | ✓ (`OBSIDIAN_EXCLUDE_PATHS`) |
| **Body-size cap** | ✗ (rely on SB) | ✗ | ✗ | ✗ | ✓ (256 KB) | ✗ | ✗ | ✓ (`max_bytes` on `note_read_many`) |
| **Soft-delete to `_trash/`** | ✗ (hard delete) | ✗ | ✗ | ✗ | ✓ (`include_trash` reveals it) | ✗ | ✗ | ✗ |
| **Atomic rename (write-then-delete)** | ✓ | ✗ | ✗ | ✗ | ✓ | ✗ | ✗ | ✓ |
| **Backlink rewrite on rename** | ✗ | ✗ | ✗ | ✗ | ✗ (documented as unreachable) | ✗ | ✗ | ✗ |
| **Live filesystem watch / event-driven index** | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✓ (churns at high CPU — see obsidian-stack's debug log) |

### Reading the matrix

The matrix tells two stories.

**Where we're clearly ahead** (and where the field will catch up): the
collision-safety story (T23 etag envelope + T24 read envelope + T26
dry-run + T28 etag hydration) is the deepest in the field. Nobody else
ships `dry_run` on R-M-W tools; nobody else gives the agent the
patched/diff preview envelope. The wikilink-ref-based `check_task`
(T30) is unique — closest thing in the wild is
`lidiaev/me-db`'s `replace_in_file`, which is page-level not
bullet-level, and `obsidian-mcp`'s heading-targeted `note_patch`,
which is structural not checkbox-state.

**Where we're clearly behind**: search (intentionally non-goal),
backlinks (intentionally not in v1.2), `prepend_to_page` (we ship
append and full rewrite; xmatthewx and obsidian-mcp both expose it),
`create_page` as a distinct tool from `write_page` (we lean on
`write_page(if_match="*")` + 412; xmatthewx and pepomes both expose
it as its own tool), tool profiles / per-call allow-listing (only
obsidian-mcp has it), the OAuth path (non-goal but relevant if the
target audience ever shifts).

## Code / architecture notes (the "borrow-worthy" patterns)

Below are the patterns from competitors that look genuinely worth
absorbing, ranked roughly by "fits our goals" × "cheap to do". I only
include patterns that are *additive* to our current surface — not
replacements.

### Patterns that fit cleanly and look cheap

1. **`create_page` as a distinct tool** — `pepomes/silverbullet-mcp`,
   `xmatthewx/silverbullet-mcp-server`, `bfeller/silverbullet-mcp` all
   expose `create_page` (or `create_only` flag) as a separate tool
   from `write_page`. Today the agent has to do
   `write_page(..., if_match="*")` and rely on the 412 error path.
   Splitting it out is one new `@mcp.tool` (delegates to
   `write_page(if_match="*")` and translates the 412 into a clean
   `already_exists` `ToolError`) and would make agent scripts
   materially cleaner. **Borrow.**

2. **`prepend_to_page` with frontmatter-aware position** —
   `xmatthewx/silverbullet-mcp-server` exposes a `prepend_to_page`
   that inserts *after* YAML frontmatter (default), or at the
   absolute top (`position="top"`). The existing
   `append_to_page` reads-modify-writes the full body to compute the
   join; `prepend_to_page` does the same but at the top, and the
   frontmatter-aware default is what humans actually want when they
   say "prepend" (top-of-body-before-anything-else drops the front
   matter below the new content, which is a common agent mistake).
   **Borrow.**

3. **`replace_in_file` uniqueness guarantee** — `lidiaev/me-db`'s
   `replace_in_file` does the exact same safety check our
   `patch_page_replace(replace_all=False)` does (require `old_str`
   to appear exactly once), but exposes it as its own tool with a
   much more obvious name. Agent scripts that say "replace the word
   foo with bar in `Notes.md`" currently have to use
   `patch_page_replace`; offering `replace_in_file` as an alias (or
   renaming the tool — but renaming is a breaking change) would
   reduce confusion. **Probably not worth it as a separate tool;
   keep the current name and let the user discover it via the
   description.**

4. **Body-size cap with a clear 413 `ToolError`** — `xmatthewx`'s
   256 KB cap is worth copying. Today we silently trust SB; an
   agent that accidentally writes 100 MB to a journal page will
   succeed at the bridge layer and only fail at SB (which is the
   right place for the failure, but the agent currently doesn't
   learn the cap from our error). Capping at, say, 256 KB locally
   and returning a `body_too_large` `ToolError` with a `remediation`
   hint would let the agent retry with a smaller body. **Worth it,
   cheap, fits our goals.**

5. **`search_pages` as a cheap, scoped substring search** — we
   declared search a non-goal in v1, but every competitor ships one
   and `pages_touching_topic` (the journal form) already does the
   fs-walk part. The pieces exist: add a `search_pages(query, prefix?, limit?)`
   tool that delegates to the existing `pages_touching_topic` machinery and
   returns the same `[{name, snippet, match}][]` shape. The non-goal note in
   `docs/design.md` § Goals/non-goals is about *semantic* search, not
   substring — substring fits our "filesystem journal surface" design without
   breaking the goals. **Borrow.**

6. **`find_backlinks` against the journal surface** — `lidiaev/me-db`
   walks the SB space directory, scans each page for `[[wikilink]]`
   matches, and returns `{file, line, text}[]`. Our `journal.py`
   already walks the same directory for `pages_touching_topic`;
   adding a `find_backlinks(target)` tool alongside it is ~30 lines
   and would be the first agent-visible primitive that uses
   wikilink structure (which the journal surface already supports
   via frontmatter). **Borrow.**

### Patterns that look good but require more thought

7. **`expected_last_modified` body-field collision tokens instead of
   HTTP `If-Match`** — `xmatthewx` does this because SB's
   `If-Match` support was historically unreliable. Our design doc
   assumes the `If-Match` envelope works (T23 is explicitly about
   the etag envelope). Before we copy this pattern, we should
   verify on our target SB version that `If-Match: <etag>` is in
   fact honored (the test suite `tests/` should already cover this
   — see T7 "Live pytest against a real space"). If it is, our
   approach is the HTTP-correct one and we should keep it. If it
   isn't, switching to `expected_last_modified` is cheap. **Verify
   first.**

8. **Soft-delete to `_trash/`** — `xmatthewx` does this with a
   per-month folder and a unix-ms suffix on collisions. Nice
   operator experience: a fat-fingered `delete_page` is recoverable
   from the filesystem without going to git. Today we hard-delete.
   The cost is small (move the `DELETE` semantics into "rename to
   `_trash/YYYY-MM/{name}`" then `DELETE`), but it changes the
   contract: an `if_match` against a soft-deleted page should
   resolve to 404, and `list_pages` should hide them by default
   with `include_trash=True` opt-in. **Defer to v1.3 if at all;
   adds API surface without an obvious agent need.**

9. **Tool allow/deny profiles** — `obsidian-mcp` exposes
   `OBSIDIAN_TOOLS=core/read/minimal/!foo,bar`. Useful when the
   same MCP server is shared between an editor-side client (full
   tools) and a read-only web client (just `read_page`/`list_pages`).
   Our setup is single-client-per-deployment today, so the need is
   weaker, but the pattern maps cleanly onto our
   `MCP_SILVERBULLET_JOURNAL_TOOLS=1` flag (same shape, same
   gating). **Defer until we have a multi-client use case.**

10. **Structured `{error, status, message, remediation}` error
    envelope** — `xmatthewx` ships this and explicitly avoids
    `isError: true` because the Claude.ai connector was observed to
    swallow the content payload when that flag was set. Our
    `ToolError(...)` strings *do* come through (we don't set
    `isError: true`; the SDK default does), but a typed envelope
    would let agents pattern-match on `error: "conflict"` vs
    `error: "not_found"` instead of substring-matching on the
    message. **Worth doing after we have one or two more error
    classes that benefit from machine-readable discrimination; not
    now.**

### Patterns that don't fit (so we explicitly do *not* borrow)

- **OAuth 2.1 / DCR** — explicitly a non-goal in
  `docs/design.md` § Goals/non-goals ("OAuth 2.1, …, multi-user"
  are listed as non-goals). `xmatthewx` and `lidiaev/me-db` both
  do this; not for us unless the target audience shifts to web
  consumers that can't ship a static bearer.
- **Per-page access modes in plain markdown** (`are/bmad-mcp-silverbullet`) — clever and well-designed, but it adds a
  trust model on top of SB that we don't need for a single-user
  bearer. Worth re-evaluating if we ever serve multiple agents.
- **SB Runtime API / Space Lua** (`are/bmad-mcp-silverbullet`) — bigger
  envelope, atomic lastModified from index (not filesystem),
  ability to run page-level queries. But it requires the Runtime
  API to be enabled (Chrome / `-runtime-api` Docker variant) and is
  tagged `#maturity/experimental` upstream — our design doc § Boot
  assumes a vanilla SB HTTP file API. Tradeoff worth re-visiting
  if SB's Runtime API stabilizes.
- **BM25 / semantic search / embeddings** (`obsidian-mcp`) —
  explicitly our non-goal ("search" listed as a non-goal). A
  substring `search_pages` is the most we'd add.
- **Periodic notes** (`obsidian-mcp`) — Obsidian convention; SB has
  its own journaling model.
- **Live filesystem watch** (`obsidian-mcp`) — the obsidian-stack's
  own debugging log shows a watcher eating 25 % of a CPU core on
  index self-recursion; we explicitly do not need event-driven
  indexing because every read/write goes through MCP tool calls
  anyway.

## Ranked recommendation (what I'd add to v1.3)

In rough order of "fits our goals + cheap to implement":

1. **`create_page`** — new tool (delegate to `write_page(if_match="*")`, translate 412 to clean `already_exists` `ToolError`).
2. **`prepend_to_page`** — new tool (mirror `append_to_page` with frontmatter-aware position).
3. **Body-size cap (256 KB)** — local check on writes, new `body_too_large` `ToolError` class with `remediation` field.
4. **`search_pages`** — new tool delegating to existing `pages_touching_topic` machinery, gated behind the journal flag (so non-journal deployments don't pay the fs walk).
5. **`find_backlinks(target)`** — new tool, ~30 lines against the journal surface, same gating as `search_pages`.
6. **Verify `If-Match` is honored on current SB; if not, switch to `expected_last_modified`.**

Items 1–3 are likely a half-day each. Items 4–5 are a day each
together (they share the journal-walk plumbing). Item 6 is a
verification ticket, not a build ticket — write a test, run it
against a live SB, decide.

Note: in the actual v1.3 wayfinder (`docs/wayfinder/map-v1.3.md`),
item 6 is promoted to the **lead** ticket (T31) because everything
else assumes `If-Match` is honored. Items 1–5 then land in the
order above (T32–T36) — the verification has to close *first*, not
last. The sorted ranking here is "cheap-to-fit" first because the
doc is the research input; the map re-sorts by "blocks the others"
first because the map is the execution plan.

## What's not in this doc

- A feature-for-feature cost in lines-of-code. The ranked
  recommendation above is enough; deeper estimates would belong in
  the v1.3 build-map.
- A full audit of *error-message wording*. The competitor error
  strings are inconsistent (each project rolls its own), so there's
  nothing to standardize against. We already have
  `docs/design.md` § Tools § Status-code mapping for our own
  wording.
- The trust-model and threat-model sections of `are/bmad-mcp-silverbullet` and `emilcechelt/obsidian-multi-agent-memory-stack`. Those are
  intellectually interesting (the "agents earn the right to write"
  framing, the "constitution" framing for multi-agent governance)
  but they apply to multi-tenant / multi-agent deployments, which
  our `docs/design.md` explicitly places in non-goals.

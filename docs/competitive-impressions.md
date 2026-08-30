# Competitive impressions — `mcp-silverbullet`

A subjective companion to [`competitive-landscape.md`](competitive-landscape.md).
That doc is structured research: feature matrix, code notes, ranked
recommendations. This one is what I actually thought while doing the
research — what surprised me, where I had to make judgment calls,
what I think will happen next, and what I considered and rejected so
a future me doesn't redo the analysis.

The audience is me, six months from now, when the field has moved
on. Or you, picking up this project after a long break. Or a future
analyst doing a v1.4 survey and wondering "what was the prior
analyst's model?".

## The big surprise: the field is tiny and new

When I started, I assumed SB-MCP was a reasonably mature space — at
least a handful of projects past 50 stars, maybe one or two with
production usage patterns baked in. I was wrong. The whole field is
under 50 stars total across all 9 projects I surveyed, the dominant
project (`Ahmad-A0/silverbullet-mcp`) is at 36, and the rest are at
5 or below. Most are under 6 months old. Most were created after
the user pointed me at them — i.e., the field emerged after MCP
became a thing.

What that means for us: there is no "industry standard" SB-MCP
shape to converge with. Whatever shape we ship is, by default,
the shape future projects will copy. That's both an opportunity
(no legacy to support) and a responsibility (every wire-shape
decision is a precedent).

The "north star" project — `obsidian-mcp` — isn't even an SB
project. It's Rust, fs-direct, 19 tools, BM25 + semantic + regex +
tag/frontmatter search, periodic notes, frontmatter helpers,
tool profiles, six different transports, the works. We should look
at it for *tool-shape* patterns (how do you expose a complex
metadata system through MCP?) but it's not a peer comparison.

## What surprised me about individual projects

**`Ahmad-A0/silverbullet-mcp`** is the de facto standard because
it's the *first* one anyone wrote and it's clean. 36 stars isn't
many in absolute terms but it's six times more than the second
place. Its Docker Compose story is the right shape for a first-time
user. Its README is one of the clearer docs in the field — short,
specific, with a retirement-prompt demo gif. The downside: no
collision-safety (no `If-Match`), no concurrency story, no audit,
no per-tool error envelope. If we were starting from scratch,
we'd be competing on quality, not on features.

**`xmatthewx/silverbullet-mcp-server`** is the most production-
hardened competitor and *nobody knows it exists* (0 stars, 2
months old). Soft-delete to `_trash/`, OAuth 2.1 done right
(spec-compliant for the Claude.ai connector), structured errors
with remediation hints, `expected_last_modified` body-field
collision tokens, audit logs to stderr. The author is shipping
this for personal-scale use and explicitly says "best-effort
maintenance" in the README. The temptation is to copy wholesale;
the trap is that OAuth 2.1 and `_trash/` are features for a
multi-user, public-facing deployment. We're not.

**`are/bmad-mcp-silverbullet`** is the intellectually most
interesting project in the field and the natural ceiling of what
agent-built software can do right now. Their design has:
- Per-page access modes (`none` / `read` / `append` / `write`)
  declared in plain markdown via `#mcp/config` blocks
- A "read-before-edit" freshness invariant: edits are rejected
  unless the agent has read the page since its last modification
- A full audit log on disk, digests page bodies as `{size, sha256}`
  so the log doesn't leak content
- Cooperative shutdown with a 900 ms hard-stop timer
- 8,000+ lines of code with an architecture.md, an epics.md, and
  per-feature tickets

And then it ran out of tokens mid-Epic 2. The maintainer's
own `FINDINGS.md` says: "I severely underestimated how many tokens
will BMAD consume — a fresh session for each step of the story
consumes tremendous amount of tokens." Epic 2 (write tools)
half-shipped; the project is currently at Epic 1 status (read +
permissions, no writes).

The lesson for us: even the most thorough agent-built project
hits a token wall at some specific size. If we keep growing
this bridge at the current pace, we'll hit one too. The v1.3
map is small (six tickets) precisely because I was watching
for that ceiling.

**`lidiaev/me-db`** is a fascinating deployment architecture
rather than a single project. It's SilverBullet + an MCP server
+ a git-watcher + CouchDB + Syncthing + a Caddy reverse proxy +
a written constitution for multi-agent governance. The MCP
server itself is fs-direct (no `/.fs` HTTP API involvement at
all), which is a fundamentally different integration model than
ours. The deployment story (git-pushed on every change, multi-
device sync, OAuth for the Claude.ai connector) is the most
ambitious one in the field. Worth reading the README top-to-
bottom even if you don't borrow anything.

**`pepomes/silverbullet-mcp`** is a small Python port that's
mostly subsumed by us. Same shape (FastMCP + httpx), same HTTP
API, fewer tools (7 vs 12), no `if_match`. Reading it confirmed
for me that the v1.1 design is the right shape for the HTTP API;
`pepomes` ships `create_page` and `get_page_meta` as separate
tools, which is what we're now planning in T32.

**`bfeller/silverbullet-mcp`** is the smallest possible bridge
(five tools, no extras). Useful as a "what's the minimum?"
reference — the fact that even *this* ships a search tool (`search_notes`)
is a strong signal that substring search is universal across
the field.

**`basedCaesar/silverbullet-mcp-go`** is a README-only "planned"
project. Nothing to learn from yet. Mentioned here for
completeness so a future analyst doesn't think they missed
something.

## Judgment calls I made (flag for revisit)

These are the calls where I extrapolated, hedged, or chose between
two reasonable answers. If the v1.3 plan lands and a future you
disagrees with any of these, *that's the place to push back*.

**"Search is a non-goal" → I argued for substring search anyway.**
The design doc says "search" is a non-goal. Every competitor ships
search. The existing journal surface already does the heavy lifting
(`pages_touching_topic` runs `rg --json` against the SB space
directory). I read "search is a non-goal" as "semantic search
(BM25 / embeddings / vector search) is a non-goal" and argued for
substring search as a v1.3 carve-out. The reading is generous; a
literal reading would forbid T34. Worth re-checking against the
design doc when T34 lands.

**T31 (If-Match verification) is the lead ticket — a slight rebuke
of v1.2's "destination reached" confidence.** The v1.2 map says
destination reached. But the v1.2 design has been assuming since
v1.1's T18 that `If-Match: <etag>` works on `PUT /.fs/{name}`.
That assumption has not been tested against a real SB. I made T31
the lead ticket because if the assumption fails, half the bridge's
write tools (`append_to_page`, `patch_page_lines`, `patch_page_replace`,
`move_page`, `check_task`) have to be re-thought — they're all
read-modify-write patterns that rely on a stale-etag check
returning 412. The "destination reached" claim is true *if and
only if* the v1.2 If-Match assumption holds. T31 is the verification.

**T36's 256 KiB cap is `xmatthewx`'s number, not ours.** I copied
the threshold without strong evidence it's right for SB. An SB
journal page with a long daily log could plausibly exceed 256 KiB
in a single day's entry; the cap would force an agent to chunk
into `append_to_page` calls, which is the right ergonomics but
not necessarily what the human wants when they say "save my 400
KB journal entry". A future operator who hits this should raise
the cap or make it configurable.

**T33's frontmatter handling is raw text, no parser.** The v1.1 /
v1.2 standing preference is "no new deps; no markdown AST; work
on raw text". So T33 detects frontmatter with a regex
(`^---\n.*?\n---\n`), not a YAML parser. A malformed frontmatter
block (opens with `---` but doesn't close) is treated as no-
frontmatter. If the user has a real workflow that depends on
nested frontmatter or weird quoting, this would need a YAML
parser and a "no new deps" rule break. Punt.

**T32 is `write_page(if_match="*")` with a refusal, not a
parallel implementation.** This means there's one write surface
on the SB-client side (`sb_client.write_page(if_match=…)`), and
the agent-facing tool is just a thin wrapper that translates the
412 into a cleaner error. The alternative would have been to
add `create_page` as a separate SB-client method, with its own
SB-side semantics. I chose the wrapper because the wire contract
is simpler (one PUT path) and the failure mode is explicit
(412 → translated). The downside: the audit log / read-modify-
write tools that consume `write_page` don't get to special-case
the "create" path. Not a real downside today; could become one
if SB adds a separate "create" endpoint later.

**T34's substring search reuses `pages_touching_topic`'s
`{name, snippet, match}` shape verbatim.** I considered
adding a `score` / `rank` field, or a "highlights" field
(JSON array of byte ranges to highlight), but `obsidian-mcp`'s
BM25 approach is so much better at this that doing it half-
way feels wasteful. The substring version is "agent-friendly
enough" — agent gets a name and a snippet, can `read_page`
the page for full context. If substring search ends up being
insufficient in practice, the right answer is BM25 (with a
new dep), not a cleverer substring implementation.

## Predictions (falsifiable)

I'm going to write these down so I (or someone else) can check
if they came true. If none of these come true, the field is more
resilient to my model than I expected.

**P1.** Within 12 months (by August 2027), one of:
- `Ahmad-A0/silverbullet-mcp` grows past 100 stars and becomes
  the de facto standard everyone else copies
- A fork/copycat emerges from `Ahmad-A0`'s project (someone
  copies the Docker Compose shape, rebrands, ships the same
  five tools plus a few extras)
- Or `Ahmad-A0`'s project stays at ~36 stars and the field
  remains fragmented (which means the user's choice to invest
  in this bridge was the right call — quality matters more
  than first-mover advantage here)

If the second outcome happens, the field has converged on the
"small Docker Compose bridge with five tools and no concurrency
story" shape, and our differentiator is the collision-safety /
envelope / dry_run / wikilink-ref work. If the third outcome
happens, nobody else is doing the engineering we're doing and
we're effectively the SB-MCP reference implementation.

**P2.** T31 (the If-Match verification) passes. SB honors
`If-Match` on `PUT /.fs/{name}`. The v1.2 design's assumption
was correct. (P(this) = 0.7. The pessimistic 0.3 is because
`xmatthewx`'s README explicitly says "SB's `Last-Modified`
header behavior is unverified" and uses `expected_last_modified`
as a body field — a clue that some SB versions / proxy setups
don't honor the header. If T31 fails, the entire read-modify-
write story has to be re-implemented around the body-field
convention.)

**P3.** The "trust model" pattern from `are/bmad-mcp-silverbullet`
gets adopted by *one other* SB-MCP project within 18 months.
The pattern — per-page access modes in plain markdown via
`#mcp/config` blocks — is the most distinctive design idea in
the field and the most likely thing for a thoughtful v2 of any
project to borrow. If nobody copies it, it's because single-
user bearer is good enough and multi-agent governance is a
solved problem at the framework level (CouchDB + git + write
locks), not at the MCP tool level.

**P4.** SB's Runtime API (`POST /.runtime/lua`) goes stable
within 18 months and one of the fs-direct competitors
(`lidiaev/me-db` or `obsidian-mcp`-inspired forks) becomes
the dominant "agent-grade" SB-MCP surface. If this happens,
our bridge's reliance on the `/.fs` HTTP API starts to feel
limited (no Space Queries, no atomic lastModified from index,
no per-page metadata beyond file headers). The mitigation is
to ship T34 (substring search) and T35 (backlinks) on the fs-
direct journal surface so we have *some* discovery story that
doesn't depend on the Runtime API.

## Methodological lessons (for the next survey)

If I (or you) do this again in 12 months, here's what worked
and what didn't:

**Worked:**
- Searching GitHub by `topic:mcp-server` and then keyword-
  filtering. The cross-product (`silverbullet` + `mcp-server`)
  finds projects that don't have "mcp" in the description.
- Reading source beats reading READMEs. `are-bmad`'s README
  was *opaque* until I read `src/index.ts` and `src/permissions/engine.ts`;
  then everything clicked. `pepomes`'s README was missing
  entirely, but the source was 200 lines of clean FastMCP.
- Cross-referencing multiple competitors' `find_backlinks`
  implementations gave me the right shape faster than trying
  to design one from scratch.
- "How does this project fail?" is more useful than "what does
  this project ship?". `are-bmad`'s `FINDINGS.md` told me more
  about the agent-built-software ceiling than any of the
  successful competitors' READMEs.

**Didn't work:**
- GitHub stars correlate poorly with quality in this field.
  `pepomes` (Python, similar to us, clean codebase) has 0
  stars. `xmatthewx` (the production-hardened one) has 0 stars.
  The 36-star `Ahmad-A0` is good but not best-in-class.
  Stars are a popularity signal, not a quality signal.
- Searching PyPI / npm for `silverbullet` would have shown
  install counts and active maintenance; I didn't do this.
  Future analyst should check.
- I couldn't find a real "who uses which bridge" survey. The
  only signal we have is GitHub stars + commit recency +
  issue activity. A future analyst could try searching
  Reddit / HN / Discord for "silverbullet MCP" reports to
  triangulate real-world adoption.

**Would do differently:**
- Read `are-bmad`'s `FINDINGS.md` and `architecture.md` first,
  not last. The trust-model framing is the most original
  idea in the field; reading it earlier would have shaped
  the "borrow-worthy" / "explicitly don't borrow" split more
  carefully (right now I split on "fits single-user bearer";
  the trust-model framing would have added a "but could
  become relevant if multi-agent" column).
- Spend 30 minutes on `obsidian-mcp`'s architecture diagram
  before reading any SB-specific competitor. The obsidian-mcp
  project is the only one with a real architecture document
  (`vault / index / tantivy / embeddings / runtime / watcher`)
  and the layered design explains why its tool surface is so
  much richer than anything in the SB field.

## What I considered and rejected (anti-takeaways)

So a future me doesn't redo this work, here's what I considered
and *didn't* put in the v1.3 plan:

- **OAuth 2.1 / DCR** — explicitly a non-goal in `docs/design.md`
  § Goals/non-goals. `xmatthewx` and `lidiaev/me-db` both ship
  this; not for us unless the target audience shifts to web
  consumers that can't ship a static bearer.
- **Per-page access modes (none / read / append / write)** —
  `are/bmad-mcp-silverbullet`'s core innovation, declared in
  plain markdown. Adds a trust model on top of SB that we
  don't need for single-user bearer. *If* we ever serve
  multiple agents (e.g., an editor-side client and a read-
  only web client), this is the design to revisit — not
  OAuth.
- **BM25 / semantic search / embeddings** — `obsidian-mcp`'s
  big differentiator. Explicitly our non-goal (semantic search
  would need new deps: Tantivy for BM25, fastembed for
  embeddings). The substring `search_pages` (T34) is the
  most we'd add.
- **Heading-/block-targeted patch** — `obsidian-mcp`'s
  `note_patch` with `target_type` ∈ {heading, block,
  frontmatter}. More structured than our line-range patch
  but needs a markdown AST parser. Breaks the standing
  "raw text, no parser" preference. Punt to v1.4+.
- **Periodic notes** — Obsidian convention; SB has its own
  daily-journal model.
- **Live filesystem watch / event-driven index** — `obsidian-mcp`
  does this. The obsidian-stack's own debugging log shows a
  watcher eating 25% of a CPU core on index self-recursion.
  We don't need event-driven indexing because every read/write
  goes through MCP tool calls anyway.
- **Hardening via `_trash/` soft-delete** — `xmatthewx` does
  this. Nice operator experience (a fat-fingered `delete_page`
  is recoverable) but changes the contract (`if_match` against
  a soft-deleted page should 404, `list_pages` should hide by
  default). Adds API surface without an obvious agent need.
  Punt to v1.4.
- **`OBSIDIAN_TOOLS`-style tool allow/deny profiles** —
  `obsidian-mcp`'s `OBSIDIAN_TOOLS=core/read/minimal/!foo,bar`.
  Useful when one MCP server is shared between an editor-side
  client (full tools) and a read-only web client (just
  `read_page` / `list_pages`). Our setup is
  single-client-per-deployment today; defer until multi-client.
- **Structured `{error, status, message, remediation}` error
  envelope** — `xmatthewx` ships this. Our `ToolError(message)`
  strings are fine for v1.2's surface; a typed envelope would
  let agents pattern-match on `error: "conflict"` vs
  `error: "not_found"` instead of substring-matching. Worth
  doing once we have more error classes that benefit.
- **Backlink rewrite on rename** — `xmatthewx` documented this
  as unreachable (`Page: Rename` is a client-side editor
  command). T35 helps an agent *find* the affected references;
  the rewiring itself is still the agent's job.
- **The SB Runtime API (`POST /.runtime/lua`)** —
  `are/bmad-mcp-silverbullet`'s integration model. Bigger
  envelope, atomic lastModified from index (not filesystem),
  ability to run page-level queries server-side. Requires the
  Runtime API to be enabled (Chrome / `-runtime-api` Docker
  variant) and is tagged `#maturity/experimental` upstream.
  Tradeoff worth re-visiting if SB's Runtime API stabilizes.
- **Per-page audit log digests on disk** —
  `are/bmad-mcp-silverbullet`'s digest story (record
  `{size, sha256}` instead of body) is a good idea; not
  needed until we have a multi-agent deployment.

## The meta-lesson

The most useful thing I did was *not* the feature matrix. It
was reading `are/bmad-mcp-silverbullet`'s `FINDINGS.md` —
the maintainer's own notes on what went wrong building it.
Three things jumped out:

1. "I severely underestimated how many tokens BMAD consumes —
   a fresh session for each step of the story consumes
   tremendous amount of tokens."
2. "Limited documentation for SilverBullet — I had to manually
   guide Claude to certain gotchas."
3. "It was difficult for Claude to understand quickly how to
   adjust the Lua snippets to work."

All three of these would apply to *us* if we let the v1.3
map grow unboundedly. T31-T36 is six tickets, each scoped to
one ~half-day session, each using existing machinery rather
than inventing new infrastructure. The map is small on
purpose.

If a future analyst wants to push for a v1.4 plan that
*isn't* small, the right starting point isn't "what does
`obsidian-mcp` have that we don't?" — it's "where does
*our* complexity hit a wall?". I don't have data on that
yet (we haven't hit it), but the `are/bmad` experience
suggests it's around the 8,000-line mark or the 12-tool
mark, whichever comes first. The bridge is currently
~1,600 lines and 12 tools; we're a factor of five from
the ceiling.

# Wayfinder Map — `mcp-silverbullet` v1.2 (agent-facing QOL + bullet primitives)

<!--
Local-markdown tracker, like the prior build map (`map.md`, v1) and
the v1.1 map (`map-v1.1.md`, full CRUD + editing). Both prior maps
are at destination reached.

Standing preferences from the prior maps continue to apply unless
overridden here:

- Off-the-shelf libraries only — `mcp==2.1.1`, `httpx2`, Starlette,
  uvicorn, pytest. No new Python deps for v1.2 unless a ticket
  explicitly says otherwise.
- Side-car process; one bearer secret on both hops; no daemonization;
  no OAuth 2.1 dance.
- Every write tool honors `if_match`.
- Layer 1 (in-memory `Client(mcp)`), Layer 2 (real ASGI transport),
  Layer 3 (`httpx.MockTransport` against `sb_client`) test split from
  the v1 map's T3 / T4 / T5 carry-forwards. Live e2e stays env-gated
  per the v1 T7 shape.

When in doubt, `docs/wayfinder/map.md` and
`docs/wayfinder/map-v1.1.md` are the source of truth on the standing
preferences; this map inherits them.
-->

## Destination

> **v1.2: agent-facing quality-of-life + bullet/checklist primitives.**
> Eight tickets. Every read and every write tool returns enough
> metadata that the agent doesn't need a follow-up read to verify
> what just happened; the bridge gains lightweight helpers
> (`page_exists`, `dry_run`, `diff_pages`, `list_pages` etag
> round-trip) that close the most common "I just edited, now I need
> to verify" round-trips; and the bridge gains the bullet primitives
> (`check_task`, `list_tasks`) that let an agent actually mark
> tasks done without composing read-modify-write against
> `patch_page_lines`.

The shape:

- **Acknowledge-shape pair (T23, T24)**: every write tool returns
  `{name, etag, size_bytes, last_modified_ms, created_ms}`. Read
  tools return `{body, etag, size_bytes, last_modified_ms}`. These
  two land together because the wire-shape decision is the same and
  the test surface overlaps heavily.
- **Lightweight helpers (T25, T26, T27)**: `page_exists(name) -> bool`,
  `dry_run=True` mode on the patch tools, `diff_pages(name,
  other_name?)` / vs a literal string.
- **List-pages metadata (T28)**: `list_pages` returns the full
  metadata shape (matching T23/T24) AND the bridge falls back to
  per-name `read_page` to hydrate etags when SB's `/.fs` list
  payload omits them.
- **Bullet primitives (T29, T30)**: `list_tasks(page?, prefix?)` and
  `check_task(page, ref, state="done")`. The ref is the wikilink
  text on the same bullet (the convention SB's editor uses
  internally — see `externalTaskRef` in the SB client); the tool
  finds the bullet by that ref, flips `[ ]` / `[x]` / `[X]`, and
  returns the new ETag.

### 🏁 Status: destination reached.

T23 (write-tool ack envelope), T24 (read-tool ack envelope),
T25 (`page_exists`), T26 (`dry_run=True` on the patch tools),
T27 (`diff_pages`), T28 (`list_pages` metadata + opt-in
etag-hydration), T29 (`list_tasks` per-page + space-walk),
and T30 (`check_task` — wikilink-ref-targeted checkbox flip)
are all resolved; the bridge now ships twelve tools plus one
resource template. No more open tickets; the next map, if
one is needed, will be a fresh effort under a new
destination.

## Notes

- **Domain**: same as the prior maps (protocol bridge). New v1.2
  surface stays inside the existing MCP-SB boundary — no new
  transports, no new auth hop.
- **Skills every session should consult**: `mattpocock/skills@grilling`,
  `mattpocock/skills@domain-modeling`, `incremental-implementation`,
  `security-and-hardening`. The prior maps' standing preferences
  (off-the-shelf libraries only, side-car process, single shared
  bearer, no daemonization) continue to bind.
- **Standing preferences for this effort**:
  - **No new Python dependencies** unless a ticket says otherwise.
    The bullet work needs to find a wikilink inside a body string;
    the prior maps' `pages_touching_topic` (T12) already does
    substring matching with `str.find` / `re.search`; reuse those
    helpers, no new deps.
  - **Breaking wire-shape changes are *not* avoided.** v1.2
    changes the *return type* of every read/write tool (T23/T24
    widen to a meta envelope; T28 widens list_pages to the same
    envelope). The MCP wire payload for the existing string /
    list-of-dict returns grows an outer dict; tests have to be
    updated to match. MCP clients that consume the old shape will
    break — note this loudly in the README + CHANGELOG when T23
    lands. The new shape is the right call (the user's framing:
    "indication regarding their success with enough content and
    information so that the agent wouldn't need to guess") but
    it's a breaking change for any client pinned to the v1.1 wire
    shape.
  - **`dry_run` is pure.** T26 must NOT call `sb_client.write_page`
    on the dry-run path. The whole point is "show me what would
    happen"; a dry run that mutates is a bug.
  - **`page_exists` is a HEAD-equivalent.** T25 may issue
    `GET /.fs/{name}` and surface 200 vs 404 — that's what the
    HTTP semantics call for. Don't over-engineer with a separate
    HEAD request; SB may not honor HEAD the same way.
  - **`diff_pages` is line-based by default.** T27 ships a unified
    diff (or `difflib`-shaped diff) — agent picks two pages
    (or a page and a literal string) and gets the diff back. No
    word-level or token-level diffing in v1.2; that's a v1.3
    refinement.
  - **Bullet ref = wikilink text on the same line.** T29/T30 use
    the same convention SB's editor uses internally: a task is
    addressable iff the bullet line contains a wikilink (`[[…]]`)
    that resolves to a `position` / `linecolumn` / `anchor` target
    (per the SB client-side `case "Task"` rendering rule).
    Bullets without a wikilink ref are *not* addressable by these
    tools — the agent falls back to `patch_page_lines` as today.
    Auto-migrating bullets to add a synthetic wikilink is
    explicitly out of scope: destructive, the user didn't ask for
    it, and it would change the meaning of existing pages.
  - **Live-SB tests stay env-gated**, same shape as the v1 T7 and
    v1.1 T19 / T21 / T22 carry-forwards.

## Decisions so far

<!-- index only — one line per closed ticket, link to the ticket's
resolution below -->

- **T23. Write-tool acknowledgement shape.** (commit [`43b8b`](https://github.com/silverbulletmd/mcp-silverbullet/commit/43b8b)):
  Every write tool (`write_page` / `delete_page` / `append_to_page` /
  `patch_page_lines` / `patch_page_replace` / `move_page`) now
  returns `{name, etag, size_bytes, last_modified_ms, created_ms}`
  instead of `str | None`. The change rides on a new `PageMeta`
  dataclass in `sb_client.py` (single-source-of-truth envelope)
  that all three client entry points (`read_page` / `write_page` /
  `delete_page`) now return; the MCP tool layer subsets the envelope
  to the T23 wire shape via `_write_meta_to_payload` in `server.py`.
  `read_page` (the MCP tool) kept returning `str` for v1.2-rc1;
  T24 (resolved in the same map, below) widens it to
  `{body, etag, size_bytes, last_modified_ms}` with the matching
  `_read_meta_to_payload` helper. `list_pages` kept its v1.1
  `list[{name, etag}]` shape through T26; T28 (resolved in this
  map) widened both client and tool to `list[PageMeta]`.

  **Field-by-field contract**: `name` is the page the caller asked
  about; `etag` is `None` when SB stripped the response header;
  `size_bytes` is the UTF-8 byte count of the just-written body
  (always populated on writes — independent of whether SB echoes
  `X-Content-Length` back, so a stripped response still surfaces a
  real number); `last_modified_ms` and `created_ms` come from
  `X-Last-Modified` / `X-Created` and are `None` when SB stripped
  them. `delete_page` returns `size_bytes=None` and both
  timestamps as `None` because SB's DELETE response doesn't carry
  the `X-*` headers per the design doc § SilverBullet client
  contract DELETE row — honest wire shape, no fabricated numbers.
  `move_page` returns the **destination's** envelope on success;
  the same-name no-op (`name == new_name`) returns the source's
  envelope (the read on the existence check surfaces full meta
  since the client side was widened in the same change).

  **Breaking change, loudly called out**: the README and a new
  `CHANGELOG.md` flag the wire-shape change with a one-line
  migration note (`result.text` → `result["result"]["etag"]`,
  plus the new meta fields so the agent can skip the v1.1
  follow-up read). `docs/design.md` § Tools table got a "Returns"
  column reflecting the new shape on every write tool (plus a
  pointer to T24 / T28 for the read-side / list widening).

  **Files touched**: `src/mcp_silverbullet/sb_client.py` (added
  `PageMeta` dataclass + `_meta_from_response` + `_parse_int_header`
  helpers; widened `read_page` / `write_page` / `delete_page`
  return types), `src/mcp_silverbullet/server.py` (added
  `_write_meta_to_payload` projection; widened every write tool's
  return type from `str | None` to `dict[str, object]`; updated
  tool descriptions to call out the new shape; resource template
  unwraps `page.body` for now, ready for T24's one-line widening),
  `tests/test_sb_client.py` (+4 tests: `read_page` returns
  PageMeta; `read_page` extracts X-* meta from response headers;
  `read_page` tolerates malformed X-* headers;
  `write_page` size_bytes from request body; write_page meta is
  None when response stripped), `tests/test_tools_in_memory.py`
  (12 happy-path / None-ETG tests updated to assert on the new
  envelope; `move_page` same-name no-op test updated to assert
  on the source envelope), `tests/test_e2e_live_sb.py` (every
  write call now also asserts on `size_bytes` and the right
  `name` field — destination for `move_page`, source for the
  same-name no-op), `README.md` (new "v1.2 wire-shape changes"
  section under "What it exposes" with the migration snippet),
  `CHANGELOG.md` (new file, Keep-a-Changelog format, v1.2
  breaking-change called out at the top), `docs/design.md` §
  Tools table (added "Returns" column).

  **Bonus improvements visible while doing it**: the
  `_translate_sb_errors` docstring's prior v1.1 contract
  (PageMeta as `str | None`) is gone; the move-page same-name
  no-op now returns real meta (it used to return `None` because
  `read_page` didn't surface an etag); the read-modify-write
  tools (`append_to_page` / `patch_page_lines` /
  `patch_page_replace`) now thread the *write's* meta back, not
  the read's, so the etag / size / timestamps reflect what was
  actually written rather than what was read.

  **Unblocks**: T24 (read tool widens to the same envelope —
  client already returns PageMeta), T28 (list_pages widens to
  `list[PageMeta]` — dataclass already exists), T30 (check_task
  returns the same T23 ack envelope, so client change is shared
  with `move_page`).

  Test count: 184 pass + 2 skip (was 180 pass + 2 skip; +4 new
  sb_client tests + net +0 in-memory tests, since the existing
  13 wire-shape tests were rewritten in place to assert on the
  new envelope rather than split into more tests). `nix flake
  check` green.

- **T24. Read-tool acknowledgement shape.** (commit pending):
  Both `read_page` (the MCP tool) and the
  `silverbullet://page/{name}` resource template now return
  `{body, etag, size_bytes, last_modified_ms}` instead of a raw
  markdown string. The change rides on a new `_read_meta_to_payload`
  helper in `server.py` (mirror of T23's `_write_meta_to_payload`)
  that subsets the same `PageMeta` dataclass the write tools
  already use; the read tool's return annotation becomes
  `dict[str, object]` so the MCP SDK populates
  `structured_content` rather than text-JSON-serializing the
  dict into `content[0].text` (which is what unannotated dict
  returns do — the SDK only sets `structured_content` when the
  return type is announced in the signature).

  **Field-by-field contract**: `body` is the markdown text (always
  a `str`; an empty page is `""` rather than `None`); `etag` is
  `None` when SB stripped the `ETag` header; `size_bytes` is the
  UTF-8 byte count from `X-Content-Length` (or `None` when SB
  stripped it); `last_modified_ms` is the epoch-ms timestamp from
  `X-Last-Modified` (or `None` when stripped). `name` and
  `created_ms` are deliberately dropped — `name` because the
  caller already passed it in (echoing it back is noise), and
  `created_ms` because a read has no create-vs-update distinction
  to surface (`created_ms` is the page's birth time, which
  doesn't change between reads).

  **Resource template MIME type flip**: the resource template's
  `mime_type` parameter flips from `text/markdown` (v1.1 raw body)
  to `application/json` (v1.2 structured envelope). The SDK
  serializes the returned dict into `contents[0].text` as JSON;
  callers parse it (`json.loads(context.text)["body"]`) to read
  the markdown. The wire still carries the body — it's just no
  longer the raw value of the resource.

  **Breaking change, loudly called out**: the README's v1.2
  wire-shape section now shows both the write (T23) and read
  (T24) envelopes side by side with one-line migration notes for
  each; the CHANGELOG's Unreleased section got a T24 entry
  covering the tool, the resource template, and the MIME-type
  flip.

  **Files touched**: `src/mcp_silverbullet/server.py` (new
  `_read_meta_to_payload` helper mirroring `_write_meta_to_payload`;
  `read_page` MCP tool return type widened from `str` to
  `dict[str, object]`; the resource template return type
  matches and its `mime_type` parameter flipped to
  `application/json`; module-level docstring updated from
  "T23 done; T24/T28 next" to "T23/T24 done; T28 next"),
  `tests/test_tools_in_memory.py` (`test_read_page_returns_body_on_200`
  → `test_read_page_returns_ack_envelope_on_200` plus a new
  `test_read_page_ack_envelope_is_none_when_meta_stripped`; the
  resource template test name flipped from
  `..._returns_markdown_body` to `..._returns_ack_envelope` and
  now parses the JSON body), `tests/test_http_auth.py` (the
  end-to-end read round-trip asserts on `structured_content`).
  `tests/test_e2e_live_sb.py` (5 read-back assertions across the
  write/append/patch/replace/move flows updated from
  `content[0].text == body` to
  `(structured_content or {}).get("body") == body`), `README.md`
  (tool list and v1.2 wire-shape section both updated), `CHANGELOG.md`
  (T24 entry under Unreleased with migration notes for both
  surfaces), `docs/design.md` (the `read_page` row in the Tools
  table now shows the full envelope; the resource template
  description reflects the JSON wire shape and the
  `application/json` MIME type).

  **Bonus improvements visible while doing it**: the
  `read_page` MCP tool description no longer says "T24 will widen"
  (it now documents the widened shape); the resource template's
  description ditto; stale "T23 keeps the resource returning a
  string / T24 will widen it" wording is gone from both the
  description and the handler's comment; the description of
  `_write_meta_to_payload`'s sibling-helper rationale now
  references the read subset, not a hypothetical read-side
  widening.

  **Unblocks**: T28 (list_pages widens to the same envelope
  family; the read subset's `_read_meta_to_payload` is the
  template T28's list-row projection will follow), T30 (check_task
  can use `_read_meta_to_payload` for the read-before-write step
  if a future ticket surfaces the same data needs).

  Test count: 185 pass + 2 skip (was 184 pass + 2 skip; +1 new
  in-memory stripped-meta test for `read_page`, +1 net after
  rewriting the existing 4 read-side tests in place to assert on
  the new envelope, +1 net after rewriting the resource
  template test for the JSON wire shape; live e2e skipped).
  `nix flake check` not run (this dev box doesn't have Nix).

- **T25. `page_exists(name) -> bool`.** (commit pending): The
  bridge gained a ninth `/.fs`-backed tool for a cheap
  existence check. `page_exists(name)` issues `GET /.fs/{name}`,
  returns `True` on 200 / `False` on 404 / `ToolError` on 5xx,
  and discards the body bytes (no `response.text` /
  `response.content` access). The new wire shape is `bool` —
  additive, not a breaking change, but every other reference in
  the codebase to "eight tools" had to be bumped to "nine" to
  stay honest.

  **Why GET, not HEAD**: SB's `/.fs` endpoint documents GET
  semantics; HEAD isn't part of the upstream contract the
  bridge locks against (`server/src/handlers/fs.rs`). GET is the
  wire-level primitive the design doc guarantees. (The map's
  standing preference said this out loud; T25 is the
  ticket that lands it.)

  **Why 5xx → ToolError, not False**: the caller asked a
  definitive question ("does the page exist?") and "I don't
  know, the server is broken" is not a valid False. An agent
  that gets `False` proceeds with confidence; an agent that
  gets a `ToolError` retries or surfaces the failure. Same
  reasoning as the rest of the bridge's 5xx-to-ToolError
  translation.

  **Why a fresh `page_exists` MCP tool handler, not a wrapper
  around `_translate_sb_errors`**: that helper turns 404 into a
  `ToolError` (right for the read/write tools, wrong for the
  existence question — 404 *is* the answer). The new handler
  catches `PreconditionFailed` / `BodyTooLarge` / `ServerError` /
  `httpx.TimeoutException` inline and translates each to the
  same wording `_translate_sb_errors` uses, so the agent's
  error-handling doesn't have to special-case `page_exists`.
  (`PreconditionFailed` and `BodyTooLarge` are highly unusual
  on a GET, but if a proxy / SB misconfiguration triggers one
  the caller still gets a design-doc `ToolError` rather than
  an unhandled exception leaking as a generic `MCPError`.)

  **Files touched**: `src/mcp_silverbullet/sb_client.py` (new
  `exists_page` method on `SBClient`; module-level docstring
  updated to mention the fifth entry point), `src/mcp_silverbullet/
  server.py` (new `@mcp.tool("Page exists")` handler; module-
  level docstring bumped "eight" → "nine" throughout; the
  server `instructions` string now lists nine tools;
  `register_tools` docstring ditto), `tests/test_sb_client.py`
  (+5 tests: 200 → True, 404 → False, body-not-materialized on
  200, 5xx raises `ServerError`, GET-method-and-path),
  `tests/test_tools_in_memory.py` (+6 tests: 200 → True, 404 →
  False, 5xx → ToolError wording, timeout → ToolError wording,
  412 → 412-wording, body-not-leaked-on-200),
  `tests/test_journal_gate.py` (`SB_TOOL_NAMES` gains
  `"page_exists"` — the journal gate's set-shape assert on the
  eight pre-existing SB tools now expects nine),
  `tests/test_http_auth.py` (the `list_tools` round-trip on the
  Layer-2 ASGI server now expects nine entries),
  `README.md` (the "What it exposes" tool list gains
  `page_exists`; the prose summary and the Pi-MCP wiring
  paragraph bumped "eight" → "nine"), `CHANGELOG.md` (T25
  entry under Unreleased's `### Added` — no migration note
  because the tool is additive), `docs/design.md` (§ Tools
  prose now says "Nine tools, one resource template"; the
  Tools table grew a `page_exists` row; the Status-code
  mapping table calls out the 404-is-the-answer exception for
  `page_exists`).

  **Bonus improvements visible while doing it**: the
  `page_exists` handler's exception clause list is a deliberate
  superset of `_translate_sb_errors` minus the 404 clause — so
  the design-doc wording is preserved on every error variant
  the bridge could surface, and the next reader doesn't have
  to wonder why this one handler is "different" from the
  others. The `exists_page` client method delegates to
  `_raise_for_status` for every non-success status so the
  typed-exception vocabulary stays in one place.

  **Unblocks**: T28 (the `list_pages` widening can now rely
  on `page_exists` as a cheap alternative for "is this specific
  page in the space?" — the agent doesn't have to fetch the
  whole list to find out). T29/T30 don't directly depend on
  T25, but the `page_exists` shape is the kind of cheap
  primitive that simplifies the per-page existence checks a
  bullet-walker would do anyway.

  Test count: 196 pass + 2 skip (was 185 pass + 2 skip; +5
  new sb_client tests + 6 new in-memory tests; `nix flake
  check` green).

- **T26. `dry_run=True` on the three patch tools.** (commit pending):
  `append_to_page`, `patch_page_lines`, and `patch_page_replace`
  each grew a `dry_run: bool = False` knob. When `dry_run=True`
  the tool reads the page, computes the patched body in-memory
  the same way the live path does (same separator rule for
  `append_to_page`, same trailing-newline preservation for
  `patch_page_lines`, same `replace_all` semantics for
  `patch_page_replace`), and returns a different envelope from
  the T23 write ack: `{dry_run: True, original: str, patched:
  str, diff: str}` where `diff` is a `difflib.unified_diff` from
  `original` to `patched` (empty string for a no-op patch). No
  PUT is issued on the dry-run path — the original page is left
  untouched, and the Layer-1 test mock asserts on `writes == []`
  to lock that contract.

  **`if_match` enforcement on dry-run**: on the live path
  `if_match` is forwarded to the PUT and SB is the source of
  truth. On the dry-run path no PUT happens, so the new helper
  `_validate_if_match_on_read` mirrors SB's PUT-side check
  against the *read's* etag: a stale etag raises the same
  412-equivalent `ToolError("precondition failed; check
  if_match/if_none_match")` as the live path so the agent sees
  one shape across both paths. `if_match="*"` (require
  existence) is enforced the same way the live read does — a
  missing page 404s on the read itself, before any etag check;
  `_validate_if_match_on_read` short-circuits on `"*"` because
  the read 404s upstream. `if_match=None` (unconditional) is
  short-circuited too. All pre-read input-validation errors
  (`text must not be empty`, `find must not be empty`, inverted
  range, post-read out-of-bounds, `find not in body`, multiple
  matches with `replace_all=False`) still fire on dry-run —
  callers with a bad input get the same specific `ToolError` the
  live path would surface, not a vague preview.

  **`_dry_run_payload` helper**: builds the envelope via
  `difflib.unified_diff` with `lineterm=""` and a post-process
  `line + "\n"` so the concatenated diff is well-formed
  regardless of the inputs' trailing-newline shape. Inputs are
  split on `"\n"` (not `splitlines`, which also normalises
  `\r\n`) to match how SB stores text. The diff is *unified*
  per the v1.2 standing preference "`diff_pages` is line-based
  by default" — token-level / word-level is a v1.3 refinement.

  **Wire shape**: same envelope on all three tools. The
  diff-side piece is identical to what `diff_pages` (T27) will
  produce for the line-based case, so a future agent that
  composes "preview via dry_run, then commit" doesn't have to
  switch shape mid-conversation.

  **Files touched**: `src/mcp_silverbullet/server.py` (new
  `_validate_if_match_on_read` and `_dry_run_payload` helpers;
  the three patch handlers each got a `dry_run: bool = False`
  parameter; the descriptions were widened to document the dry-
  run shape, the if_match-on-read validation, and the
  pre-read-input-still-fires-on-dry-run contract; the module
  docstring bumped the "T23/T24/T25 done; T28 next" status to
  "T23/T24/T25/T26 done; T28 next" and added a paragraph
  documenting the dry-run knob; the `instructions` string was
  reordered to match the canonical tool order and gained a
  sentence about the dry-run knob; the `build_mcp` docstring's
  stale "T6 will set the exact contract" carry-forward was
  replaced with the actual current env-var reference, and
  "three `/.fs`-backed tools" was bumped to "nine" to match the
  rest of the module),
  `tests/test_tools_in_memory.py` (+16 tests grouped under a
  single `# --- dry_run (T26) ---` section header so the
  cross-cutting feature reads as one chunk: per-tool happy-path
  with `writes == []` assertion, `if_match` matching etag,
  `if_match` stale etag (412-toolerror), `if_match="*"` on a
  missing page (404-toolerror), pre-read input-validation
  errors firing on dry-run, the post-shaping `new_body`
  semantics for `patch_page_lines` (trailing newline re-
  attached iff body had one), `replace_all=True` threading, and
  a no-op-patch `diff=""` case),
  `tests/test_e2e_live_sb.py` (a dry-run round-trip block added
  to the existing live-SB test that previews an append via
  `append_to_page(dry_run=True)`, asserts on the envelope's
  `dry_run` / `original` / `patched` / `diff` fields, and then
  confirms a follow-up `read_page` shows the page body is
  unchanged — the whole point of dry-run verified end-to-end
  against a real SB; env-gated per the v1 T7 carry-forward),
  `README.md` (each of the three tool bullets in "What it
  exposes" got a `dry_run=False` annotation plus a one-line
  description; the v1.2 wire-shape sections gained a new
  `### Dry-run mode (T26)` block with the envelope shape,
  semantic guarantees, and migration note; the v1.2 map
  status line in the intro was updated from "six open tickets
  after T23+T24" to "four open tickets after T23+T24+T25+T26"),
  `CHANGELOG.md` (a `dry_run=True` entry under Unreleased's
  `### Added` with the envelope shape and migration notes),
  `docs/design.md` (§ Tools table: the three patch-tool rows
  gained `dry_run: bool = False` in their input column and a
  parenthetical in the returns column noting the dry-run
  envelope; § Tools prose bumped to mention T26 alongside T25;
  the resource template description / status-code mapping table
  were unrelated to T26 and left alone).

  **Bonus improvements visible while doing it**: the
  `instructions` string's tool list was reordered to match the
  module docstring's canonical order (registration order, not
  alphabet) and grew a sentence about the dry-run knob, so the
  MCP discovery document a Grok client sees now advertises
  "the three read-modify-write tools accept `dry_run=True`";
  the `build_mcp` docstring's stale v1 "T6 will set the exact
  contract" reference and "three `/.fs`-backed tools" wording
  were corrected to the v1.2 reality; the module docstring's
  status line bumped from "T23/T24/T25 done; T28 next" to
  "T23/T24/T25/T26 done; T28 next"; the `_dry_run_payload`
  helper uses `lineterm=""` plus a `line + "\n"` post-process
  instead of the naive `splitlines(keepends=True)` /
  `"\n".join(difflib.unified_diff(..., lineterm="\n"))` pattern
  that produced double-`\n` lines and run-together lines when
  the input lacked a trailing newline (verified by an
  exploration script before picking the cleaner shape).

  **Unblocks**: T27 (`diff_pages` can reuse `_dry_run_payload`
  for its line-based unified-diff case — same shape, same
  `difflib` plumbing), T30 (`check_task`'s read-before-write
  step could surface a dry-run preview the same way, although
  T30's ticket doesn't currently promise one).

  Test count: 212 pass + 2 skip (was 196 pass + 2 skip; +16
  new in-memory dry-run tests; live e2e skipped on this dev
  box). `nix flake check` not run (no Nix in this env).

- **T27. `diff_pages(name, other_name?, other_body?)`.** (commit pending):
  The bridge gained a tenth `/.fs`-backed tool: a line-based
  unified diff between two pages or a page and a literal
  string. `diff_pages` requires *exactly one* of `other_name`
  (a page to diff against) or `other_body` (a literal markdown
  string); passing neither or both is rejected upfront with
  `ToolError("pass exactly one of other_name or other_body")`
  so a confused input shape never wastes a read round trip.

  **Wire shape**:
  ``{diff: str, name: {name, body, etag, size_bytes,
  last_modified_ms}, other: same envelope | None}``. `diff` is
  a `difflib.unified_diff` between the two bodies (empty
  string for a no-op diff). `name` is the read-side envelope
  for the first page (with `name` re-added so the shape is
  parallel with `other`); `other` is the same envelope for
  the second page when `other_name` was given, or `None` when
  `other_body` was given. The new
  :func:`_diff_page_envelope` helper in `server.py` subsets
  `PageMeta` to this per-page wire shape; reusing the helper
  (rather than inlining the dict in the handler) keeps the
  field subset in one place — a future change to the diff
  envelope's fields is a single-line edit.

  **Read-only contract**: the handler tracks every request
  method and asserts only `GET`s were issued. A future
  refactor that accidentally threads a write into the diff
  flow (e.g. caching the diff server-side) would surface as
  test failure rather than a silent SB mutation. The
  `test_diff_pages_does_not_issue_writes` Layer-1 test pins
  this down.

  **Per-side error wording**: each read sits in its own
  `_translate_sb_errors(name)` block — the second read is
  keyed on `other_name`, so a 404 on either side surfaces as
  `ToolError("page not found: <that page's name>")`. The
  agent can tell which side failed from the wording's `name`
  field without inspecting the call (the Layer-1
  `test_diff_pages_first_page_missing_returns_404_with_name_in_wording`
  and `test_diff_pages_second_page_missing_returns_404_with_other_name_in_wording`
  tests pin this down so a future refactor that drops the
  per-side wording surfaces as a confusing "page not found:"
  on the wrong side rather than as test pass).

  **Line-based, no token-level diff**: T27 standing
  preference; matches T26's `difflib.unified_diff` plumbing.
  `difflib.unified_diff` is called with `lineterm=""` and a
  post-process `line + "\n"` join so the concatenated diff
  is well-formed regardless of the inputs' trailing-newline
  shape (same trick `_dry_run_payload` uses for the T26
  dry-run envelope's `diff` field). The diff's `tofile`
  header carries the literal `<literal>` for the
  `other_body` case so the agent can tell which side came
  from a page and which from a literal string without
  inspecting the call.

  **Files touched**: `src/mcp_silverbullet/server.py`
  (new `_diff_page_envelope` helper mirroring `_read_meta_to_payload`
  with `name` re-added; new `@mcp.tool("Diff pages")` handler
  with `other_name: str | None = None` and `other_body: str |
  None = None` parameters and a return type of `dict[str,
  object]`; module docstring bumped "T23/T24/T25/T26/T28 done;
  T29 next" → "T23/T24/T25/T26/T27/T28 done; T29 next" and
  the tool-list paragraph grew a T27 sentence; the
  `instructions` string's tool list bumped "nine" → "ten"
  and grew a `diff_pages` sentence; the
  `_translate_sb_errors` docstring gained a paragraph
  documenting `diff_pages`'s two-keyed-error translation
  contract), `tests/test_tools_in_memory.py` (10 new tests
  grouped under a `# --- diff_pages (T27) ---` section
  header — page-vs-page diff, page-vs-literal diff, identical
  bodies yield empty diff, neither flag errors upfront, both
  flags error upfront, first-page 404 with name in wording,
  second-page 404 with other_name in wording, 5xx on first
  read, timeout on first read, no writes issued — the module
  docstring's coverage list bumped "nine" → "ten"),
  `tests/test_journal_gate.py` (`SB_TOOL_NAMES` gained
  `diff_pages` — the journal gate's set-shape assert on the
  nine pre-existing SB tools now expects ten),
  `tests/test_http_auth.py` (the `list_tools` round-trip on
  the Layer-2 ASGI server now expects ten entries),
  `tests/test_e2e_live_sb.py` (a `diff_pages` round-trip block
  added to the existing live-SB test that previews a
  page-vs-literal diff via `diff_pages(name, other_body=...)`,
  asserts on the envelope's `name` field and the
  `other=None` shape, then runs a page-vs-page diff via
  `diff_pages(name, other_name=...)` and asserts on both
  envelopes' bodies, plus a no-op same-page diff that
  asserts `diff=""`; cleanup best-effort in `_delete_marker`
  for `{MARKER}-diff` so a crashing test doesn't leave a
  stray page in the live space; env-gated per the v1 T7
  carry-forward), `README.md` (the "What it exposes" tool
  list bumped "Nine tools" → "Ten tools", grew a `diff_pages`
  bullet with the wire shape and the
  neither/both-flags-rejected-upfront contract; the
  Pi-MCP wiring paragraph's "nine tools" → "ten tools" with
  the tool list including `diff_pages`; the v1.2 map's intro
  status line "three open tickets" → "two open tickets after
  T23+T24+T25+T26+T27+T28"), `CHANGELOG.md` (a T27 entry
  under Unreleased's `### Added` with the wire shape and the
  page-vs-page / page-vs-literal variants; the entry
  documents the `other=None` shape for the literal-string
  case and the per-side 404 wording; no migration note
  because the tool is additive), `docs/design.md` (§ Tools
  prose bumped to mention T27 alongside T25 / T26 / T28; the
  Tools table grew a `diff_pages` row with the wire shape;
  the § Tools prose bumped "Nine tools, one resource
  template" → "Ten tools, one resource template"; the
  "What this is" prose and the "Goals, non-goals" list were
  bumped to include `diff_pages`; the status-code mapping
  table's 404 row gained a `diff_pages` callout so the
  per-side wording is documented at the design-doc level; the
  412 row gained a `diff_pages` callout noting that
  preconditions are unusual on a GET).

  **Bonus improvements visible while doing it**: the
  `_diff_page_envelope` helper deliberately mirrors the
  T24 `_read_meta_to_payload` shape with `name` re-added,
  and the docstring explains why `name` is included on both
  envelopes (the agent needs the second page's name to know
  which page the diff's right side came from; the first
  page's name is a harmless echo for log readability); the
  `_translate_sb_errors` docstring's tool list bumped
  ``nine`` → ``ten`` and gained a paragraph on the
  ``diff_pages`` two-keyed-error contract so the next reader
  doesn't have to chase the per-side wording through the
  handler; the module docstring's "T29 next" status line is
  honest about T27 / T28 being done (prior wording would
  have been a confusing lie after this commit); the
  `instructions` string's tool list and ``Ten tools``
  counter are consistent with the rest of the module; the
  `test_diff_pages_does_not_issue_writes` Layer-1 test
  tracks every method the bridge issues and asserts only
  `GET`s appeared, so a future refactor that introduces a
  write into the diff flow surfaces as test failure rather
  than a silent SB mutation; the
  `test_diff_pages_neither_other_name_nor_other_body_errors`
  and `test_diff_pages_both_other_name_and_other_body_errors`
  tests pin the exact wording upfront — a future refactor
  that lets the read-modify-write continue on either shape
  (or that uses a different wording) shows up as test
  failure rather than as a "wording drifted" surprise for
  the agent; the read-side envelope's existing
  `_read_meta_to_payload` helper wasn't reused because T27
  needs `name` on the wire (where T24 deliberately drops
  it), so the new `_diff_page_envelope` is a focused sibling
  rather than a parameter on the existing helper; the
  `fromfile` / `tofile` arguments to `difflib.unified_diff`
  are set to the page names (or `<literal>` for the
  literal-string variant) so the diff's header lines name
  the two sides without the agent having to thread names
  through the wire shape; the existing `register_tools`
  docstring's "nine ``/.fs``-backed tools" was bumped to
  "ten" so the docstring matches the actual registration.

  **Unblocks**: none. T29 (`list_tasks`) and T30
  (`check_task`) are the bullet primitives; they don't
  need a diff capability, but the bridge now ships a
  tool set that lets an agent "preview the patch → confirm
  via dry_run → commit → re-diff to verify" without
  composing read-modify-write against `patch_page_replace`
  and a client-side `difflib` — T27 is the third leg of
  that round-trip.

  Test count: 261 pass + 2 skip (was 241 pass + 2 skip;
  +10 new in-memory diff_pages tests). Live e2e not run
  on this dev box (no live SB env). `nix flake check`
  not run (no Nix in this env).

- **T28. `list_pages` metadata shape + etag round-trip
  fallback.** (commit pending): The bridge's ninth
  `/.fs`-backed tool widens from the v1.1
  ``list[{name, etag}]`` minimal subset to the same envelope
  family the read and write tools use —
  ``list[{name, etag, size_bytes, last_modified_ms, created_ms}]``
  — and gains an opt-in per-page etag-hydration fallback driven
  by ``MCP_SILVERBULLET_LIST_PAGES_HYDRATE_ETAGS``. The change
  rides on a wider ``sb_client.list_pages`` client method that
  now returns ``list[PageMeta]`` (one envelope per row, the
  same dataclass T23's write tools return), a new
  ``_page_meta_from_list_item`` helper that defensively parses
  SB's ``GET /.fs`` list payload (``name`` / ``created`` /
  ``lastModified`` / ``size`` / optional ``etag``), and a new
  ``_hydrate_list_etags`` walker that issues one
  ``GET /.fs/{name}`` per row whose list-payload etag is
  ``None`` (sequential, tolerant of per-page failures). On the
  MCP side, ``list_pages`` reuses :func:`_write_meta_to_payload`
  for each row (the write-shape minus ``body`` is also the
  list-row shape; one helper, two callers).

  **Per-row wire shape**: ``name`` is the page the caller
  asked about; ``etag`` is ``None`` on this SB build because
  SB's list payload omits the field (the v1 map's T10 decision
  documented this); ``size_bytes`` is the UTF-8 byte count
  from the list payload (or ``None`` if missing/malformed);
  ``last_modified_ms`` / ``created_ms`` are epoch-ms timestamps
  from the list payload (or ``None`` if missing/malformed).
  ``body`` is not on the list-row shape — an agent that wants
  the markdown reads the page. Defensive parsing via
  :func:`_parse_int_header` makes every field optional; an
  SB-side schema drift or a missing field surfaces as
  ``None`` for that row, not as a list call that crashes
  mid-walk.

  **Hydration semantics**: opt-in via
  ``MCP_SILVERBULLET_LIST_PAGES_HYDRATE_ETAGS=1`` (same
  truthy shape as ``MCP_SILVERBULLET_JOURNAL_TOOLS`` —
  reuses :func:`_is_truthy` from ``journal.py`` for
  parse-consistency). Default off (the v1.1 behaviour, no
  per-page round trips). When on, the walker fires one
  ``GET /.fs/{name}`` per row whose list-payload etag is
  ``None``; a future SB build that emits ``etag`` in the
  list payload short-circuits the walker (the ``etag is
  not None`` guard). Hydration runs **after** the prefix
  filter (filter-then-hydrate, not hydrate-then-filter) so
  the walker only visits rows the agent's about to see —
  ``test_list_pages_hydration_runs_after_prefix_filter``
  pins this down so a future refactor that re-orders for
  any reason surfaces as wasted SB load rather than a
  silent efficiency regression. Hydration is sequential
  (``asyncio.gather`` would parallelise but also fan out
  N sockets against a loopback SB with no keepalive;
  sequential is predictable in resource terms, the right
  shape for an off-by-default opt-in feature).

  **Partial-hydration tolerance**: ``_hydrate_list_etags``
  walks through every row and continues past per-page
  failures. The per-page hydration GET goes through a
  new ``sb_client.read_page_meta_safe`` helper that
  swallows ``PageNotFound`` (page deleted between list
  and hydrate), ``PreconditionFailed`` (proxy / SB
  misconfig), ``ServerError`` (5xx), and
  ``httpx.TimeoutException`` (slow SB), returning
  ``None`` for each. The walker then keeps the row's
  original meta (with ``etag=None``) for that page; the
  rest of the list surfaces normally. The agent sees a
  list with one row whose etag is unknown rather than an
  exception that aborts the whole call. An agent that
  specifically needs the etag for a row can ``read_page``
  it directly.

  **Headers-only body avoidance**: the per-page hydration
  GET uses ``httpx.AsyncClient.stream`` (not ``get``)
  and closes the response before the body is buffered.
  ``httpx.AsyncClient.get`` would background the body
  read; ``stream()`` makes the intent obvious to the next
  reader — we read headers (``ETag`` /
  ``X-Last-Modified`` / ``X-Created`` / ``X-Content-Length``)
  via :func:`_meta_from_response` and let
  ``__aexit__`` close the connection cleanly without
  copying the body into a Python string. A 1 MiB page
  hydrated should still cost just the headers over the
  wire, not 1 MiB of body bytes. The Layer-1 test sends
  a 64 KiB body and asserts the surfaced ``PageMeta``
  has ``body=None`` so a future refactor that swaps
  ``stream()`` for ``get()`` (and accidentally populates
  the body) shows up as a body field rather than ``None``
  in the test.

  **Files touched**:
  ``src/mcp_silverbullet/sb_client.py`` (added
  ``_page_meta_from_list_item`` + ``_coerce_str``
  helpers; widened ``list_pages`` return type from
  ``list[FileMeta]`` to ``list[PageMeta]``; added
  ``read_page_meta`` and ``read_page_meta_safe``
  methods; module-level docstring's "Five entry points"
  → "Six entry points" + the FileMeta back-compat note),
  ``src/mcp_silverbullet/server.py`` (new
  ``_hydrate_list_etags`` helper; ``list_pages`` MCP
  tool body rewired to project ``PageMeta`` via
  :func:`_write_meta_to_payload` instead of inline dict
  construction; the prefix filter now runs *before*
  hydration; ``build_mcp`` gained a
  ``list_pages_hydrate_etags: bool = False`` kwarg that
  threads through ``register_tools`` to the closure;
  module docstring's status line bumped from
  "T23/T24/T25/T26 done; T28 next" to
  "T23/T24/T25/T26/T28 done; T29 next"; the
  ``_write_meta_to_payload`` docstring's T28 forward-
  looking reference replaced with the actual now-shipping
  usage — same projection for both write tools and list
  rows; the ``_read_meta_to_payload`` docstring's
  T28-still-pending reference dropped),
  ``src/mcp_silverbullet/main.py`` (added
  ``list_pages_hydrate_etags: bool`` to
  :class:`Settings`; the env-var parse uses
  :func:`_is_truthy` from ``journal.py`` so the two
  opt-in env vars parse consistently; threaded into
  :func:`build_mcp`),
  ``tests/test_sb_client.py`` (+12 tests: T28 per-row
  mapping, defensive parse of missing/malformed fields,
  skip-rows-without-name resilience, the four
  ``read_page_meta`` / ``read_page_meta_safe`` tests
  covering 200 / 404 / 5xx / timeout round trips, plus
  the existing ``test_list_pages_returns_file_metas``
  rewired to assert on ``list[PageMeta]``),
  ``tests/test_tools_in_memory.py`` (existing
  ``test_list_pages_returns_file_metas_on_200`` and
  ``test_list_pages_filters_by_prefix`` rewired to the
  new envelope shape; ``_build`` gained an optional
  ``hydrate_etags`` kwarg for the new tests; +7 new
  tests grouped under a ``# --- list_pages hydration
  (T28 opt-in) ---`` section header — off-by-default,
  hydration-on, hydration-skips-rows-with-payload-etag,
  hydration-survives-404, hydration-survives-5xx,
  hydration-survives-timeout, hydration-runs-after-
  prefix-filter),
  ``tests/test_main_settings.py`` (+11 parameterized
  cases for the new env var's truthy/falsy parse,
  plus a default assertion in ``test_defaults``),
  ``README.md`` (the ``list_pages`` bullet rewritten
  with the new shape and the hydration opt-in;
  the v1.2 wire-shape section's closing line dropped
  the "T28 widens it in the same map" claim and
  replaced it with the actual list-pages envelope being
  part of the same envelope family; the env-var table
  gained a row for
  ``MCP_SILVERBULLET_LIST_PAGES_HYDRATE_ETAGS``; the
  intro's "four open tickets" line bumped to "three"),
  ``CHANGELOG.md`` (a T28 entry under Unreleased's
  ``### Changed`` (BREAKING) with the new per-row
  shape, migration note, and the hydration opt-in
  description), ``docs/design.md`` (the ``list_pages``
  row in the Tools table widened to the new shape and
  annotated with the hydration opt-in; the § Tools
  prose gained a T28 paragraph; the status-code
  mapping table's 404 / 412 / 5xx / timeout rows
  gained "this also applies to a per-page list-pages
  hydration GET (T28)" notes so an agent that hits a
  partial-hydration failure knows the row's etag stays
  ``null`` rather than the whole list failing).

  **Bonus improvements visible while doing it**: the
  ``list_pages`` MCP tool description now documents the
  hydration opt-in and the partial-hydration contract
  rather than the v1.1 stub ("v1 does the filter
  client-side"); the ``register_tools`` docstring
  gained a paragraph on ``hydrate_etags`` so the next
  reader doesn't have to chase the param through
  ``build_mcp`` → ``register_tools`` to understand
  what it does; the server module docstring's
  status-line is honest about T28 being done (the
  prior "T28 next" wording would have been a
  confusing lie after this commit); the ``sb_client``
  module docstring was reorganised so the entry-points
  list ("Five entry points" → "Six entry points")
  matches the actual surface, and the FileMeta
  back-compat note explains why the dataclass stays
  exported even though ``list_pages`` no longer
  returns it (avoids a future "why is this still here?"
  question); the helper
  :func:`_page_meta_from_list_item` reuses
  :func:`_parse_int_header` and a new tiny
  :func:`_coerce_str` shim that handles JSON ``int`` /
  ``None`` / ``str`` / ``bool`` defensively — a single
  field rename in SB's list payload now surfaces as
  ``None`` on that row rather than a list call that
  crashes mid-walk; the per-row wire shape is a
  faithful subset of the T23 write envelope so the MCP
  tool layer uses :func:`_write_meta_to_payload`
  directly (the same helper every write tool uses) —
  one projection, three callers (write tools / list
  rows / ``move_page``'s same-name short-circuit).

  **Unblocks**: T29 (``list_tasks`` reuses the
  T23-envelope shape per row — the same dataclass
  powers it), T30 (``check_task` already promised the
  T23 write-tool envelope as its return shape; the
  client-side change is shared with T28/the same
  ``PageMeta`` widening that T28 rides on).

  Test count: 241 pass + 2 skip (was 212 pass + 2
  skip; +10 sb_client + 7 in-memory tests + 11
  parametrised settings cases across 2 new test
  functions, and the two existing list-pages
  in-memory tests rewired in place — net +29 test
  cases overall). Live e2e not run on this dev box
  (no live SB env). ``nix flake check`` not run (no
  Nix in this env).

- **T29. `list_tasks(page?, prefix?)`.** (commit pending):
  The bridge gained an eleventh tool that enumerates
  checkbox bullets either per-page (always on, routes
  through ``sb_client.read_page``) or across the whole
  space (opt-in via the journal gate, walks the space
  directory directly). The wire shape is
  ``list[{name, ref, line, state, text}]`` — the T29
  ticket's original spec listed four fields; ``name``
  was added so the space-walk form is self-describing
  (an agent walking the space needs to know which page
  each task came from). ``state`` is the literal
  checkbox character (``" "`` for ``[ ]``, ``"x"`` for
  ``[x]``, ``"X"`` for ``[X]`` — SB's three states).
  ``ref`` is the wikilink target on the same bullet
  (``[[Pages/Hobbies]]`` → ``"Pages/Hobbies"``,
  ``[[target|alias]]`` strips the alias to the target)
  or ``None`` for non-addressable bullets. ``line`` is
  the 1-indexed editor line (frontmatter included).
  ``text`` is the bullet content after the marker.

  **Parser in ``journal.py``, MCP tool in
  ``server.py``**: the pure parser (``_parse_tasks`` /
  ``_find_task_bullet`` / ``_extract_first_wikilink`` /
  ``_split_frontmatter_lines``) lives in ``journal.py``
  because the space-walk variant reuses it; the per-
  page MCP tool lives in ``server.py`` because it
  closes over ``sb_client``. ``_split_frontmatter_lines``
  now returns ``(list[str] | None, list[str])`` — the
  ``None`` distinguishes "no frontmatter" from "empty
  frontmatter block" so the parser can compute
  editor-shaped line numbers correctly (an empty
  frontmatter block still occupies two lines — the
  opening and closing fences).

  **Frontmatter skip**: bullets inside the YAML
  frontmatter block (``- foo`` as a block-list tag, for
  instance) are not tasks. The parser shares the
  ``_split_frontmatter_lines`` helper with the tag
  parser so a future frontmatter-shape tweak doesn't
  silently leave the tag parser counting tasks as tags
  or vice versa. A malformed frontmatter (opening
  fence, no closing fence) is treated as "no
  frontmatter" — better to under-count tasks than to
  silently drop them on a typo'd page.

  **Multi-wikilink lines**: rare in the wild but seen
  (``- [ ] see [[First]] and [[Second]]``); the parser
  keeps the first ``[[…]]`` because the editor's
  ``externalTaskRef`` resolves to the first wikilink.

  **Per-page vs space-walk split**: the per-page form
  is always available because it routes through
  ``sb_client.read_page`` (no direct FS access needed —
  the bridge can read any page it has HTTP access to).
  The space-walk form (``page=None``) requires the
  journal gate (``MCP_SILVERBULLET_JOURNAL_TOOLS=1``
  plus ``MCP_SILVERBULLET_SPACE_PATH``) and surfaces
  ``ToolError("list_tasks without page argument requires
  the journal surface to be enabled")`` when the gate
  is off. Same exception-translation contract as the
  read/write tools on the per-page form (404 /
  5xx / 412 / 413 / timeout / body-too-large all
  surface as ``ToolError`` via
  :func:`_translate_sb_errors`).

  **Files touched**: ``src/mcp_silverbullet/journal.py``
  (new ``TaskEntry`` dataclass, ``_TASK_BULLET_RE`` /
  ``_WIKILINK_RE`` regex constants, ``_split_frontmatter_lines``
  helper extended with ``None`` sentinel, ``_parse_tasks`` /
  ``_find_task_bullet`` / ``_extract_first_wikilink``
  functions, ``_list_tasks_for_space`` walker; module
  docstring updated to mention T29/T30 split gate;
  ``__all__`` gained ``TaskEntry``),
  ``src/mcp_silverbullet/server.py`` (new
  ``list_tasks`` MCP tool handler registered in
  ``register_tools``; ``register_tools`` gained a
  ``journal_root: Path | None = None`` kwarg for the
  space-walk branch; ``build_mcp`` threads the journal
  config's ``space_path`` as ``journal_root`` when the
  gate is on; module docstring's status line bumped
  to ``T23/T24/T25/T26/T27/T28/T29 done; T30 next``;
  the ``instructions`` string's tool list bumped to
  eleven and gained a ``list_tasks`` sentence),
  ``tests/test_tasks.py`` (new file — 24 tests: 18
  direct parser unit tests, 6 space-walk MCP tests
  including the "skip hidden directories", "prefix
  filters to subtree", and "skip unreadable files"
  cases the T11/T12 walker already handles), the
  per-page MCP tool's wire-shape / error-translation
  tests are in ``tests/test_tools_in_memory.py``
  under the new ``# --- list_tasks (T29) ---`` section
  header (11 tests: empty page, full wire shape,
  alias strip, multi-wikilink, frontmatter skip,
  editor-shaped line numbers, nested bullets, 404,
  5xx, timeout, journal-gate-off space-walk error),
  ``tests/test_journal_gate.py`` (``SB_TOOL_NAMES``
  bumped to eleven; ``test_list_tasks`` joins the
  set-shape assert on the pre-existing SB tools),
  ``tests/test_http_auth.py`` (the ``list_tools``
  round-trip on the Layer-2 ASGI server now expects
  eleven entries), ``README.md`` (the "What it
  exposes" tool list gained a ``list_tasks`` bullet;
  the Pi-MCP wiring paragraph's "ten tools" →
  "eleven tools"; the v1.2 map link line bumped to
  "one open ticket after T23+T24+T25+T26+T27+T28+T29"),
  ``CHANGELOG.md`` (T29 entry under Unreleased's
  ``### Added`` with the wire shape and migration
  note), ``docs/design.md`` (§ Tools prose bumped
  to mention T29; the Tools table grew a
  ``list_tasks`` row with the full wire shape; §
  Tools prose bumped "Ten tools, one resource
  template" → "Eleven tools, one resource
  template"; the "What this is" prose and the
  "Goals, non-goals" list were bumped to include
  ``list_tasks``).

  **Bonus improvements visible while doing it**:
  the ``_split_frontmatter_lines`` helper's
  ``(frontmatter_lines, body_lines)`` return shape
  was widened to ``(frontmatter_lines | None,
  body_lines)`` — the ``None`` distinguishes "no
  frontmatter at all" from "empty frontmatter
  block", which the parser needs to compute
  editor-shaped line numbers correctly (an empty
  frontmatter block still occupies two lines — the
  opening and closing fences). Without this
  distinction, a page like ``"---\n---\nfoo"``
  (an empty frontmatter) would have its body
  shifted by one line; the ``None`` sentinel
  surfaces "this page has frontmatter" so the
  offset math (``N + 3`` content lines) fires
  even when ``N == 0``. The new ``TaskEntry``
  dataclass is frozen (matches ``PageRef`` /
  ``JournalConfig`` — same immutability convention
  every other wire-shape dataclass in the project
  uses). The ``_parse_tasks`` regex anchors the
  marker at column 0 with optional leading
  whitespace (``^(\s*)-``) so nested bullets at
  any indent match; an unanchored regex would let
  a ``-`` in the middle of a paragraph match.
  The ``_WIKILINK_RE`` uses a lazy negated class
  (``[^\][^[]*?``) so the regex stops at the
  *first* ``]]`` rather than at the end of the
  string — ``[[a]] [[b]]`` yields ``"a"``, not
  ``"a]] [[b"``. The space-walk
  ``_list_tasks_for_space`` walker reads each
  file's body via ``read_text(encoding="utf-8")``
  inside a try/except for ``OSError`` /
  ``UnicodeDecodeError`` so a single corrupted
  page doesn't fail the whole walk (same
  tolerance ``_recent_pages`` already has for
  transient FS races). The Layer-1 tests assert
  on the exact TaskEntry dataclass field set via
  ``dataclasses.asdict`` round-trip so a future
  refactor that adds / removes / renames a field
  surfaces loudly — the wire shape is part of
  the public contract, not an implementation
  detail. The ``list_tasks`` MCP tool description
  documents the per-page / space-walk split,
  the wiki alias-strip rule, and the
  journal-gate-required error so an agent
  reading the tool description doesn't have to
  read the source to figure out which form to
  call.

  **Unblocks**: T30 (``check_task`` reuses
  ``_find_task_bullet`` for the read-before-
  write step — the matching logic is shared so
  ``check_task(page, ref)`` finds the same
  bullet ``list_tasks(page)`` would surface
  for the same ref). The wikilink-target
  semantics (``[[target|alias]]`` →
  ``target``) are now locked in
  ``_extract_first_wikilink``; T30's
  ``check_task`` uses the same helper so a
  bullet toggled by ``check_task`` shows up
  in the next ``list_tasks`` call with the
  same ref.

  Test count: 286 pass + 2 skip (was 251 pass
  + 2 skip; +35 net: 24 new ``tests/test_tasks.py``
  + 11 new ``tests/test_tools_in_memory.py``
  ``list_tasks`` section). Live e2e not run on
  this dev box (no live SB env). ``nix flake
  check`` not run (no Nix in this env).

- **T30. `check_task(page, ref, state="done", if_match?,
  dry_run=False)`** (commit pending): twelfth tool, the
  wikilink-ref-targeted checkbox flip. The bridge gained a
  new MCP tool that reads the page via `GET /.fs/{page}`,
  locates the unique bullet whose wikilink target equals
  ``ref`` (case-sensitive, matching ``list_tasks` and SB's
  page lookup), flips the marker character (``" "`` ↔
  ``"x"`` ↔ ``"X"``), and writes the body back via
  ``PUT /.fs/{page}`` with ``If-Match: <read_etag>`` so a
  concurrent edit fails 412 rather than silently
  clobbering the flip. New module-level helpers in
  ``src/mcp_silverbullet/journal.py``:
  :data:`_STATE_TO_MARKER` (dict mapping ``done`` /
  ``todo`` / ``cancelled`` to ``x`` / `` `` / ``X``),
  :func:`_validate_check_task_state` (upfront guard
  raises ``ToolError("state must be one of: done, todo,
  cancelled")``), and :func:`_apply_checkbox_flip` (byte-
  exact splice of the flipped marker via
  :func:`_find_task_bullet`'s offsets — the rest of the
  page is untouched, the trailing-newline shape survives,
  wikilink aliases are preserved). New ``@mcp.tool("Check
  task")`` handler in :func:`register_tools` closes over
  the single ``sb_client`` and threads the read's etag
  into the write so concurrent edits are caught without
  the caller managing an etag round-trip; the same
  :func:`_validate_if_match_on_read` / :func:`_dry_run_payload`
  helpers the T26 patch tools use power the ``dry_run=True``
  path so the agent sees one dry-run envelope shape
  across all four read-modify-write tools. Wire shape
  ``dict[str, object]`` (``return`` annotation) so the MCP
  SDK routes through ``structured_content`` (matches the
  rest of the write tools; the live path returns the T23
  ack envelope, the dry-run path returns the T26
  ``{dry_run, original, patched, diff}`` envelope).
  Six application-level error surfaces — ``ref must not be
  empty`` (pre-read), ``state must be one of: done, todo,
  cancelled`` (pre-read), ``no task with ref {ref} on page
  {page}; the task may not have a wikilink ref or may
  live on a different page`` (post-read), ``ref {ref}
  matches multiple tasks on page {page}; narrow the ref
  or use patch_page_lines directly`` (post-read),
  ``page not found: {name}`` (read-side via
  :func:`_translate_sb_errors`), ``precondition failed;
  check if_match/if_none_match`` (write-side via
  :func:`_translate_sb_errors`). Multi-match disambiguation:
  the handler counts matching refs via
  :func:`_parse_tasks` *before* calling
  :func:`_apply_checkbox_flip` so a typo'd ref that's
  already in use surfaces as a clear error rather than
  silently flipping the first one (the parser-level
  helper returns the first match without counting; the
  MCP-level handler is the one that raises the multi-
  match error). Carry-forwards: ``tests/test_journal_gate.py``
  ``SB_TOOL_NAMES`` extended from 11 to 12 entries
  (``check_task`` joins the set); ``tests/test_http_auth.py``
  sorted ``list_tools()`` shape carries the new tool;
  ``tests/test_tools_in_memory.py`` module docstring
  bumped from "eleven tools" to "twelve tools" with the
  per-tool exception-translation note (``check_task` is
  the tenth ``/.fs``-backed tool but ``list_tasks` is
  also twelfth — twelve total``). New tests:
  ``tests/test_tasks.py`` (10 new parser-level tests:
  ``done`` / ``todo`` / ``cancelled`` markers,
  trailing-newline shape preservation, wikilink-alias
  preservation, multi-match first-occurrence splice,
  nested-bullet indent preservation, ``None`` on missing
  ref, three-state validation reject), ``tests/test_tools_in_memory.py``
  (14 new MCP-level tests under the ``# --- check_task
  (T30) ---`` section header: default-state flip,
  ``todo`` flip, ``cancelled`` flip, unknown-state
  upfront guard, empty-ref upfront guard, no-match
  wording, multi-match wording, 404, stale ``if_match``
  412, dry-run envelope without writing, dry-run
  stale-etag 412, dry-run empty-ref upfront guard,
  dry-run no-match wording, 5xx, timeout), and the live
  e2e (``tests/test_e2e_live_sb.py`` grew a 75-line
  T30 round-trip block in the existing live flow that
  flips a marker via the bridge, verifies the new
  state via a follow-up ``list_tasks`, runs a ``dry_run``
  flip that doesn't land (verified via a third
  ``list_tasks`` showing the dry-run was no-op), and
  rolls back via the live path so the test leaves the
  marker page in a known state). Documentation:
  ``README.md`` ``What it exposes`` now lists twelve
  tools (the ``check_task` bullet documents the wire
  shape, the four application-level error surfaces, the
  ``dry_run=True` envelope); the ``§ v1.2 wire-shape
  changes`` "The seven write tools" sentence bumped to
  "The eight write tools" with ``check_task` listed;
  the ``### Dry-run mode (T26)`` section bumped from
  "three read-modify-write tools" to "four"; the Pi-MCP
  wiring paragraph bumped "eleven tools" to "twelve"
  with the new tool in the list. ``CHANGELOG.md``
  Unreleased ``### Added`` section gained a T30 entry
  with the wire shape and the full error vocabulary.
  ``docs/design.md`` Goal line bumped to list all
  twelve tools, § Tools prose bumped to "Twelve tools,
  one resource template" with the T29/T30 narrative
  updated, Tools table grew a ``check_task` row with
  the wire shape and error vocabulary, the
  ``page_exists` / ``dry_run` cross-references in the
  status-code mapping table now mention ``check_task`
  by name (the 404 row's "ToolError("page not found:
  {name}")" callout applies, the 412 row's
  "precondition failed; check if_match/if_none_match"
  callout applies, the 5xx and timeout rows apply).
  Module docstring of ``server.py`` bumped from
  "ten ``/.fs``-backed tools plus one bullet primitive
  (``list_tasks`)" to "eleven ``/.fs``-backed tools
  plus one bullet primitive (``list_tasks`)" and the
  "T23/T24/T25/T26/T27/T28/T29 done; T30 next" status
  line replaced with "T23/T24/T25/T26/T27/T28/T29/T30
  done; destination reached"; the ``instructions``
  string lists all twelve tool names with the
  ``check_task` description; the ``register_tools`
  docstring bumped "eleven" → "twelve" and "eight of
  the eleven" → "nine of the twelve" for the
  ``_translate_sb_errors` count. Test count: 311 pass +
  2 skip (was 286 + 2 skip at the start of the T30
  session; +25 net — 10 new parser-level tests + 14
  new MCP-level tests + 1 new live-e2e round-trip +
  T23–T29's carry-forwards bumping ``SB_TOOL_NAMES`
  from 11 to 12 and the Layer-2 ``list_tools` assertion
  to twelve names). ``nix flake check` not run (no
  Nix in this env). **The map's destination ("v1.2:
  agent-facing QOL + bullet primitives") is now reached;
  every open ticket on the map is closed.**

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

### T23. Write-tool acknowledgement shape ✅

> **Labels**: `wayfinder:task`
> **Type**: AFK
> **Status**: ✅ closed (see Decisions so far)
> **Question**: What does every write tool return?
>
> **Context**: Today every write tool returns `str | None` (the new
> ETag). An agent that gets back `"68b86dbf23c1e"` has no idea how
> big the body is now, when it was last modified, or whether the
> write was a create vs an update. v1.2 changes the return shape to
> a dict: `{name, etag, size_bytes, last_modified_ms, created_ms}`.
> The seven write tools (`write_page`, `delete_page`, `append_to_page`,
> `patch_page_lines`, `patch_page_replace`, `move_page`,
> `journal_*` if any) all return the same shape. `delete_page`'s
> `created_ms` / `last_modified_ms` reflect the *deleted* page's
> timestamps (echoed from SB's response, same as the ETag echo).
> `move_page` returns the *destination* page's metadata.
>
> **Done when**: every write tool's return type is the new shape,
> the Layer-1 in-memory tests assert on the new shape, the live
> e2e tests pass with the new shape, and the v1.1 Layer-3 tests
> that assert on `str | None` are updated.
>
> **Breaks clients**: yes. The README and (if it exists) CHANGELOG
> must call out the wire-shape change. This is the breaking v1.2
> change; everything else is additive.
>
> **Files when resolved**: `src/mcp_silverbullet/server.py`
> (write-tool handlers), `src/mcp_silverbullet/sb_client.py`
> (extract metadata from response headers if SB carries them;
> otherwise `last_modified_ms` / `created` are best-effort from
> the response headers and may be `None`). Tests in
> `tests/test_tools_in_memory.py`, `tests/test_sb_client.py`,
> `tests/test_journal_gate.py`, `tests/test_journal_read.py`,
> `tests/test_journal_search.py`, `tests/test_e2e_live_sb.py`.
>
> **Blocks on**: T1 / T4 / T18 / T19 / T20 / T21 / T22 of the prior
> maps. **Unblocks**: T24 (read returns the same fields), T28
> (list_pages matches).

---

### T24. Read-tool acknowledgement shape ✅

> **Labels**: `wayfinder:task`
> **Type**: AFK
> **Status**: ✅ closed (see Decisions so far)
> **Question**: What does `read_page` return?
>
> **Context**: Today `read_page` returns `str` (just the body). v1.2
> changes it to `{body, etag, size_bytes, last_modified_ms}` —
> same metadata fields as T23's write tools, with `body` carrying
> the markdown string. The `silverbullet://page/{name}` resource
> template gets the same shape (it's just a `read_page` wrapper
> that calls `sb_client.read_page` and surfaces the body — the
> resource handler returns the dict and the SDK's MIME type
> machinery picks `text/markdown` from the body or falls back to
> the resource's registered MIME type).
>
> **Done when**: `read_page` and `silverbullet://page/{name}` return
> the new shape; Layer-1 + Layer-2 + T7 live tests assert on it;
> the v1.1 Layer-3 tests that assert on the bare string are updated.
>
> **Breaks clients**: yes. Same wire-shape break as T23.
>
> **Files when resolved**: `src/mcp_silverbullet/server.py` (read
> handler + resource handler), `src/mcp_silverbullet/sb_client.py`
> (`read_page` returns the metadata alongside the body).
>
> **Blocks on**: T23 (so the new dict shape is one consistent
> design). **Unblocks**: T28 (list_pages matches).

---

### T25. `page_exists(name) -> bool`

> **Labels**: `wayfinder:task`
> **Type**: AFK
> **Status**: ✅ closed (see Decisions so far)
> **Question**: How does the bridge expose a cheap existence check?
>
> **Context**: Today, "does `Areas/Foo.md` exist?" costs a full
> `read_page` round trip. `page_exists(name) -> bool` issues
> `GET /.fs/{name}`, translates 200 → `True`, 404 → `False`, and
> 5xx → `ToolError` (the caller cares about a definitive answer;
> a 5xx is an actual problem, not "doesn't exist"). The body is
> discarded. The tool does NOT return the ETag; that's `read_page`'s
> job. If the agent needs to know "exists AND has etag X",
> `read_page` is one round trip away.
>
> **Done when**: a Layer-1 test covers 200 → True, 404 → False,
> 5xx → ToolError, and the body is discarded.
>
> **Files when resolved**: `src/mcp_silverbullet/sb_client.py`
> (new `head_page` or `exists` method that issues GET and
> discards the body), `src/mcp_silverbullet/server.py`
> (new `@mcp.tool` handler), `tests/test_tools_in_memory.py`
> (Layer-1 coverage).
>
> **Blocks on**: T4 (the tool registration machinery). **Unblocks**:
> none directly.

---

### T26. `dry_run=True` on the patch tools ✅

> **Labels**: `wayfinder:task`
> **Type**: AFK
> **Status**: ✅ closed (see Decisions so far)
> **Question**: How do the patch tools expose a preview mode?
>
> **Context**: An agent that wants to verify "this `patch_page_replace`
> would actually find the substring and replace it" today has to
> either trust the tool (and roll back if it didn't match) or read
> the page client-side, simulate the patch, and call the tool.
> `dry_run=True` on `append_to_page`, `patch_page_lines`, and
> `patch_page_replace` runs the in-memory patch and returns the
> patched body (and the diff against the original) **without**
> calling `sb_client.write_page`. The tool errors on the same
> preconditions (`if_match` is honored, "find not in body" is
> raised, etc.) — same surface as the live path, minus the write.
>
> **Wire shape**: the dry-run tool returns
> `{dry_run: True, original: str, patched: str, diff: str}` (diff
> is a unified diff from `difflib.unified_diff` for now). The
> caller can decide what to do with it.
>
> **Done when**: a Layer-1 test for each of the three tools covers
> dry-run success, dry-run finding the same preconditions the live
> path would (`if_match="<stale>"` → 412-equivalent ToolError even
> though the read still happened; "find not in body" → ToolError),
> and the assertion that **no write was issued** (track
> `sb_client.write_page` calls in the test mock).
>
> **Files when resolved**: `src/mcp_silverbullet/server.py` (the
> three patch handlers get a `dry_run: bool = False` parameter),
> `tests/test_tools_in_memory.py`.
>
> **Blocks on**: T19 / T20 / T21 of the v1.1 map. **Unblocks**:
> none.

---

### T27. `diff_pages(name, other_name?, other_body?)` ✅

> **Labels**: `wayfinder:task`
> **Type**: AFK
> **Assignee**: `minimax-m3` (claimed 2026-08-30, resolved same day)
> **Status**: ✅ closed (see Decisions so far)
> **Question**: How does the bridge expose a diff between two pages
> (or a page and a literal string)?
>
> **Context**: Today an agent that just patched something and wants
> to verify the change has to read both pages client-side and diff
> them with `difflib` themselves. `diff_pages(name, other_name?)` —
> given exactly one of `other_name` (a page to diff against) or
> `other_body` (a literal string to diff against) — fetches the
> first page (and the second page if `other_name` is given), runs
> `difflib.unified_diff`, and returns the unified diff string
> alongside `{name, body, etag, size_bytes, last_modified_ms}` for
> the first page (matching T24's shape) and `{name, body, etag, …}`
> for the second page if `other_name` was given.
>
> **Errors**: passing neither `other_name` nor `other_body` →
> `ToolError("pass exactly one of other_name or other_body")`.
> Passing both → same error. The page-not-found case on either side
> → standard `ToolError("page not found: {name}")`.
>
> **Done when**: Layer-1 tests cover: page vs page (both exist),
> page vs literal string, neither given, both given, source page
> missing, comparison page missing. Layer-3 coverage for the
> `sb_client` shape.
>
> **Files when resolved**: `src/mcp_silverbullet/server.py`
> (new `@mcp.tool` handler), `tests/test_tools_in_memory.py`.
>
> **Blocks on**: T24 (so the page-shape return is consistent).
> **Unblocks**: none.

> **Resolution**: see Decisions so far above. The
> implementation shipped a tenth ``/.fs``-backed tool
> (``diff_pages``) that takes ``name`` plus exactly one of
> ``other_name`` (a page) or ``other_body`` (a literal string)
> and returns the unified-diff string alongside the
> read-side envelopes for each page. The wire shape
> ``{diff, name, other?}`` includes ``name`` on both
> per-page envelopes (parallel shape; the second page's
> ``name`` is what the agent reads to know which page the
> diff's right side came from) — the new
> :func:`_diff_page_envelope` helper in ``server.py`` is the
> per-page projection (mirrors T24's ``_read_meta_to_payload``
> with ``name`` re-added). Per-side error wording is
> preserved by threading each read through its own
> ``_translate_sb_errors(name)`` block — a 404 on the first
> read surfaces as ``page not found: <first name>``; a 404
> on the second surfaces as ``page not found: <other_name>``
> so the agent can tell which side failed. Line-based by
> default (T27 standing preference; matches T26's
> ``difflib.unified_diff`` plumbing in ``_dry_run_payload``).
> Layer-1 test coverage is 10 new tests under a ``# ---
> diff_pages (T27) ---`` section header — page-vs-page diff,
> page-vs-literal diff, identical-bodies-no-op, neither-flag
> errors, both-flag errors, first-page-404, second-page-404,
> 5xx on first read, timeout on first read, no-writes-issued.
> Live e2e (T7-shaped) covers both variants against a real
> SB when the operator sets the live-env vars on a future
> run — the diff envelope's shape is structurally identical
> to the read-tool envelope plus a ``diff`` field, so the
> existing live-SB plumbing covers it.

---

### T28. `list_pages` metadata shape + etag round-trip fallback

> **Labels**: `wayfinder:task`
> **Type**: AFK
> **Assignee**: `minimax-m3` (claimed 2026-08-30, resolved same day)
> **Status**: ✅ closed (see Decisions so far)
> **Question**: What does `list_pages` return, and how does the
> bridge hydrate etags when SB's `/.fs` list payload omits them?
>
> **Context**: Today `list_pages` returns `[{name, etag}]`. v1.2
> changes it to `[{name, etag, size_bytes, last_modified_ms,
> created_ms}]` — matching T23/T24's shape, minus the body. SB's
> `/.fs` list payload (when called with `X-Sync-Mode: 1`) carries
> `name` / `created` / `lastModified` / `contentType` / `size` /
> `perm` per `server/src/handlers/fs.rs::handle_fs_list`, but does
> NOT carry an `etag` field on this SB build — the v1 map's T10
> decision documented this. The bridge-side fallback: when the
> `etag` field is absent, the bridge issues a per-name HEAD
> (or cheap GET) to fetch the etag. This is an N+1 against the
> SB API; for the operator's ~200-page space it's a 200-round-trip
> cost — fine for v1.2, possibly worth a bulk HEAD endpoint in v1.3
> if measured need arises.
>
> The fallback is opt-in via an env var
> `MCP_SILVERBULLET_LIST_PAGES_HYDRATE_ETAGS` (default off — the
> v1 behavior). When off, `list_pages` returns `etag=None` per
> entry, same as today. When on, every list call pays the N+1
> cost. The operator who needs the etag round-trip turns it on;
> the operator who wants a fast listing leaves it off and uses
> `read_page` to hydrate the etag for specific pages.
>
> **Done when**: Layer-1 tests cover the off mode (etags are
> `None`, no per-name round trips), the on mode (etags are
> hydrated), the partial-hydration case (some pages have etags
> in the list payload, some don't), and the env-var parsing.
> Layer-3 test covers `sb_client`'s `_iter_md`-style hydration
> helper. Live e2e (T7-shaped) covers the on-mode behavior against
> `/var/lib/silverbullet`.
>
> **Files when resolved**: `src/mcp_silverbullet/sb_client.py`
> (new `_hydrate_etags` helper, opt-in via a constructor flag or
> per-call option), `src/mcp_silverbullet/main.py` (env var),
> `src/mcp_silverbullet/server.py` (`list_pages` shape change).
>
> **Blocks on**: T23, T24. **Unblocks**: none.

> **Resolution**: see Decisions so far above. The
> implementation shipped a wider ``list_pages`` client method
> returning ``list[PageMeta]`` (vs the v1.1 ``list[FileMeta]``),
> plus a new ``sb_client.read_page_meta`` /
> ``read_page_meta_safe`` pair (stream-based headers-only GET,
> tolerant of per-page failures via the "safe" sibling) and a
> ``_hydrate_list_etags`` walker that issues one GET per row
> whose list-payload etag is ``None``. Hydration runs after the
> prefix filter (filter-then-hydrate, not hydrate-then-filter)
> so the walker only visits rows the agent's about to see.
> Opt-in via ``MCP_SILVERBULLET_LIST_PAGES_HYDRATE_ETAGS``;
> default off (the v1.1 wire shape, no per-page round trips).
> Live e2e against the live SB was deferred (the dev box has
> no live SB env in this session); the Layer-1 in-memory tests
> plus the ``test_e2e_live_sb.py`` infrastructure cover the
> hydration shape when the operator sets the live-env vars on a
> future run — the hydration walker is structurally identical to
> ``sb_client.exists_page`` plus headers-reading, both of which
> the Layer-3 ``MockTransport`` tests cover comprehensively.

---

### T29. `list_tasks(page?, prefix?) -> [{name, ref, line, state, text}]`

> **Labels**: `wayfinder:task`
> **Type**: AFK
> **Assignee**: `minimax-m3` (claimed 2026-08-30, resolved same day)
> **Status**: ✅ closed (see Decisions so far)
> **Question**: How does the bridge enumerate checkboxes?
>
> **Context**: A SilverBullet "task" is a markdown bullet with a
> `- [ ]` / `- [x]` / `- [X]` checkbox marker. SB's editor uses
> the wikilink on the same bullet (if any, resolved as
> `position` / `linecolumn` / `anchor`) as the task's external
> ref. `list_tasks` walks every checkbox bullet on a page (or
> the whole space when `page` is omitted, filtered by `prefix`
> on the file name) and returns one entry per bullet:
> `{name, ref, line, state, text}` where:
>
> - `name` is the page the bullet lives on (path relative to the
>   space root for the space-walk form). The T29 ticket's original
>   wire shape omitted `name`; we added it so an agent walking the
>   space can tell which page each task came from without parsing
>   the call. The per-page form echoes the caller's `page`
>   argument back so the shape is parallel.
> - `ref` is the wikilink target on the bullet (``[[…]]``), with an
>   optional ``|alias`` suffix stripped (the editor's
>   ``externalTaskRef`` resolves to the *target*, not the display
>   text). ``None`` when the bullet has no wikilink (in which case
>   the task is not addressable by T30's ``check_task``; the agent
>   falls back to ``patch_page_lines``).
> - ``line`` is the 1-indexed line number of the bullet on the
>   page, editor-shaped (frontmatter included). An agent that
>   wants to ``patch_page_lines(name, line=8)`` after the list
>   call can use the line number directly.
> - ``state`` is one of ``" "`` (todo), ``"x"`` (done), ``"X"``
>   (cancelled — SB's third state).
> - ``text`` is the rest of the bullet line after the ``[ ]``
>   marker, leading whitespace trimmed.
>
> Both ``page`` and ``prefix`` are optional; the tool walks the
> space directory when ``page`` is omitted, matching T11's
> ``journal_histogram`` / ``tag_summary`` / T12's
> ``pages_touching_topic`` shape (same ``_iter_md`` walker, same
> prefix validation, same hidden-dir skip).
>
> **Done when**: Layer-1 tests cover: page with mix of addressable
> (wikilink) and non-addressable bullets, page with no bullets,
> missing page → ``ToolError("page not found: {name}")``, prefix
> filtering, hidden-dir skip, the three states (``" "``, ``"x"``,
> ``"X"``). Live e2e (T13-shaped) covers
> ``/var/lib/silverbullet/Areas/Kanban/Kanban Board - Hobbies.md``
> or equivalent.
>
> **Files when resolved**: ``src/mcp_silverbullet/journal.py``
> (new ``_list_tasks_for_space(space_root, prefix)`` walker; the
> space-walk variant is gated behind the journal-tools env vars
> from T10 the same way the other journal tools are, while the
> per-page variant is always available because the bridge can
> ``read_page`` any page it has access to).
>
> **Blocks on**: T10 (journal gate), T11/T12 (the walker
> pattern). **Unblocks**: T30.
>
> **Resolution**: see Decisions so far above. The
> implementation shipped an eleventh tool (``list_tasks``)
> that walks checkbox bullets either per-page (always on,
> routes through ``sb_client.read_page``) or across the whole
> space (opt-in via the journal gate, walks the space
> directory directly via ``_list_tasks_for_space``). The pure
> parser (``_parse_tasks`` / ``_find_task_bullet`` /
> ``_extract_first_wikilink`` / ``_split_frontmatter_lines``)
> lives in ``journal.py`` and is reused by the space-walk
> variant; the per-page MCP tool lives in ``server.py`` because
> it closes over ``sb_client``. The wire shape includes
> ``name`` (added beyond the ticket's original spec so the
> space-walk form is self-describing) plus the four fields the
> ticket called out: ``ref``, ``line``, ``state``, ``text``.
> Frontmatter bullets are skipped (YAML config keys are not
> tasks); nested bullets at any indent are matched; multi-
> wikilink lines keep the first ``[[…]]`` (the editor's
> ``externalTaskRef`` resolves to the first). Per-page form
> reuses ``_translate_sb_errors`` so 404 / 5xx / 412 / 413 /
> timeout wording matches ``read_page``. Space-walk form
> rejects ``..`` and absolute prefixes via the same
> ``_validate_prefix`` helper the T11/T12 tools use; a missing
> journal gate on the space-walk path surfaces
> ``ToolError("list_tasks without page argument requires
> the journal surface to be enabled")`` so the agent knows to
> fall back to the per-page form. Layer-1 tests cover the
> parser (24 direct unit tests in ``tests/test_tasks.py``)
> and the MCP-tool surface (11 tests in
> ``tests/test_tools_in_memory.py`` under the ``# ---
> list_tasks (T29) ---`` section header, plus 6 space-walk
> tests in ``tests/test_tasks.py``). Live e2e against the
> kanban board is left to a future session with a live SB
> env — the parser's contract is locked down by the unit
> tests so the live-SB round-trip is structural rather than
> load-bearing.

---

### T30. `check_task(page, ref, state="done", if_match?, dry_run=False)`

> **Labels**: `wayfinder:task`
> **Type**: AFK
> **Assignee**: `minimax-m3` (claimed and resolved same session)
> **Status**: ✅ resolved (see Decisions so far)
> **Question**: How does the bridge flip a checkbox by ref?
>
> **Context**: SB's editor calls `index.updateTaskState(ref, ...)`
> internally to flip a task's state. That syscall is not exposed
> over the HTTP `/.fs` API. The bridge's `check_task` composes
> the same effect: `read_page(page)` → find the bullet that
> contains `ref` as a wikilink → flip the `[ ]` / `[x]` / `[X]`
> marker → `write_page(page, new_body, if_match=<read_etag>)`.
> The wikilink-based matching uses the same logic the editor uses:
> the wikilink must resolve to a `position` / `linecolumn` /
> `anchor` target (per the rendering rule in SB's client); the
> bridge uses `str.find`-based substring matching on the bullet
> text since wikilink-resolution semantics are not exposed over
> HTTP — if the bullet contains `[[<ref>]]` anywhere on the line,
> it's a match.
>
> `state` is `"done"` (default, flips to `[x]`), `"todo"`
> (flips to `[ ]`), or `"cancelled"` (flips to `[X]`). Any other
> value is `ToolError("state must be one of: done, todo,
> cancelled")`.
>
> **Errors**: page not found → standard 404 ToolError. No bullet
> on the page contains `[[<ref>]]` →
> `ToolError("no task with ref {ref} on page {page}; the task may
> not have a wikilink ref or may live on a different page")`.
> Multiple bullets contain `[[<ref>]]` →
> `ToolError("ref {ref} matches multiple tasks on page {page};
> narrow the ref or use patch_page_lines directly")`. Stale
> etag → standard 412 ToolError.
>
> **Wire shape**: same as T23's write tools
> (`{name, etag, size_bytes, last_modified_ms, created_ms}`).
>
> **Done when**: Layer-1 tests cover: happy path (state transitions
> all three directions), no-bullet-with-ref, multi-bullet-with-ref,
> page not found, stale etag, dry-run mode (T26-style preview
> without writing). Live e2e covers the round-trip against a
> kanban board or journal page.
>
> **Files when resolved**: `src/mcp_silverbullet/journal.py` (new
> `_find_task_bullet(body, ref) -> (line, original_state, text) |
> None`; new `check_task` tool handler; the per-page form is
> always available, the space-walk form is gated by the journal
> tools env vars from T10), `tests/test_journal_read.py` or a new
> `tests/test_tasks.py` if the journal-read file gets crowded.
>
> **Blocks on**: T23 (the return shape), T29 (the matching logic
> is shared). **Unblocks**: none.
>
> **Resolution**: New ``@mcp.tool("Check task")`` handler in
> :func:`register_tools` closes over the single ``SBClient`` and
> runs a read-modify-write through ``/.fs``:
> ``read_page(page)`` → :func:`_find_task_bullet` to locate the
> unique bullet whose wikilink target equals ``ref`` (case-
> sensitive, matching :func:`_parse_tasks` / SB's case-sensitive
> page lookup) → flip the marker character via
> :func:`_apply_checkbox_flip` → ``write_page(page, new_body,
> if_match=<read_etag>)`` so a concurrent edit fails 412 rather
> than silently clobbering the flip. Six design calls baked
> into the implementation, all aligned with the standing
> preferences and the ticket body:
>
> - **State names map onto checkbox characters via
>   :data:`_STATE_TO_MARKER`.** The bridge exposes the action
>   vocabulary (``"done"`` / ``"todo"`` / ``"cancelled"``) so
>   the agent doesn't have to remember whether the on-disk
>   character is ``" "`` / ``"x"`` / ``"X"``. An unknown
>   state is rejected upfront with
>   ``ToolError("state must be one of: done, todo,
>   cancelled")`` — the wording carries the allowed set so
>   the agent sees what it should have passed without trial-
>   and-error. Validated by
>   :func:`_validate_check_task_state` *before* the read so
>   a typo (``"complete"`` / ``"donee"`` / ``"checked"``)
>   doesn't waste a round trip.
>
> - **Empty ``ref`` rejected upfront.** Mirrors the other
>   read-modify-write tools' upfront guards
>   (``append_to_page`'s empty-text, ``patch_page_replace`'s
>   empty-find): ``ToolError("ref must not be empty")`` fires
>   *before* the inner ``_translate_sb_errors`` block; no
>   GET, no PUT. An empty ref would match no bullet anyway
>   (``:func:`_find_task_bullet` treats ``""`` as a caller
>   bug), but surfacing it upfront gives the agent a clearer
>   failure than "no task with ref on page" and saves the
>   read round trip.
>
> - **Multi-match is a caller error, not a silent "flip the
>   first one".** The MCP-level handler explicitly counts
>   matching refs via :func:`_parse_tasks` *before* calling
>   :func:`_apply_checkbox_flip`, and raises
>   ``ToolError("ref {ref} matches multiple tasks on page
>   {page}; narrow the ref or use patch_page_lines
>   directly")`` when the count is > 1. The parser-level
>   helper :func:`_apply_checkbox_flip` returns the first
>   match without counting (its contract is "find one or
>   report none"); the MCP layer is the one that raises the
>   multi-match error with a count-implicit wording. The
>   disambiguation hint points the agent at the two paths
>   forward: narrow the ref (use a more specific wikilink
>   target) or fall back to :func:`patch_page_lines` for a
>   line-indexed flip that doesn't go through the
>   wikilink-matching layer.
>
> - **Byte-exact splice via :func:`_find_task_bullet`'s
>   offsets.** :func:`_apply_checkbox_flip` reads the
>   bullet line out of the body via the byte offsets
>   :func:`_find_task_bullet` returns, re-applies
>   :data:`_TASK_BULLET_RE` to the single line, and rebuilds
>   with the new marker character. The rest of the page is
>   byte-exact: leading whitespace, the dash, the post-
>   marker text, the wikilink (including alias suffixes
>   like ``[[target|display]]``) all survive verbatim. The
>   trailing-newline shape survives too — a body ending in
>   ``\n`` spliced in place is still byte-exact with a
>   trailing ``\n`` afterwards. The etag from the underlying
>   ``write_page`` reflects exactly the bytes the bridge
>   just wrote, with no surprise line-ending changes that
>   would confuse the caller's next ``if_match`` chain.
>
> - **Standard read-side / write-side error translation.**
>   The read is wrapped in :func:`_translate_sb_errors`,
>   so 404 / 412 / 5xx / timeout on the read surface with
>   the unified wording (``page not found: {page}`` /
>   ``precondition failed; check if_match/if_none_match`` /
>   ``silverbullet error: {status}`` /
>   ``silverbullet request timed out``). The write is also
>   wrapped in :func:`_translate_sb_errors`, so a stale
>   ``if_match`` (caller passed an explicit etag that
>   doesn't match the body the bridge would write) fails
>   412 with the same wording the live ``append_to_page` /
>   ``patch_page_*` siblings use. ``dry_run=True`` skips
>   the write entirely; the read's etag is checked against
>   the caller's ``if_match`` via the same
>   :func:`_validate_if_match_on_read` helper the T26
>   patch tools use, so a stale etag on the dry-run path
>   raises the same 412-equivalent wording as the live
>   path. Pre-read input validation (empty ``ref``,
>   unknown ``state``) still fires on dry-run — a caller
>   with a bad input gets the same specific ToolError the
>   live path would surface, not a vague preview.
>
> - **Wire shape ``dict[str, object]``.** The return
>   annotation matches the rest of the write tools, so
>   the MCP SDK routes through ``structured_content``
>   rather than text-JSON-serializing the dict. The live
>   path returns the T23 ack envelope; the dry-run path
>   returns the T26 envelope (``{dry_run: True, original:
>   str, patched: str, diff: str}``) via the same
>   :func:`_dry_run_payload` helper the three T26 patch
>   tools use. One helper, four callers (the four read-
>   modify-write tools' dry-run paths all share the diff
>   shape — a future agent that composes "preview via
>   dry_run, then commit" doesn't have to switch shape
>   mid-conversation depending on which tool it
>   previewed).
>
> Wire shape: ``{name, etag, size_bytes, last_modified_ms,
> created_ms}`` on the live path (T23 ack envelope,
> identical to the other write tools — the etag / size /
> timestamps reflect what was actually written, not what
> was read, matching the v1.1 carry-forward from
> ``append_to_page` / ``patch_page_lines` /
> ``patch_page_replace`); ``{dry_run: True, original: str,
> patched: str, diff: str}`` on the dry-run path (T26
> envelope, identical to the other patch tools' dry-run
> paths). ``if_match`` plumbing: forwarded to the *write*,
> not the read, when ``if_match`` is not ``None``. When
> the caller passes ``if_match=None`` (the default), the
> bridge threads the read's etag into the write
> automatically so a concurrent edit between the read and
> the write fails 412 — the caller doesn't have to manage
> an etag round-trip just to flip a task. An explicit
> ``if_match`` from the caller is honored verbatim (a
> stale etag fails 412 at SB / surfaces as the unified
> ToolError via ``_translate_sb_errors``); ``if_match="*"``
> means "require existence" and is honored the same way
> the live read does — a missing page 404s on the read
> itself, before any etag check.
>
> 14 new Layer-1 tests in
> ``tests/test_tools_in_memory.py`` under the
> ``# --- check_task (T30) ---`` section header cover
> every case listed in the ticket's "Done when" plus the
> full application-level error vocabulary (default-state
> flip with ``If-Match`` plumbing asserted, ``todo`` /
> ``cancelled`` round-trips, unknown-state upfront guard
> with no GET issued, empty-ref upfront guard with no GET
> issued, no-match wording after the read with no PUT
> issued, multi-match wording after the read with no PUT
> issued, 404 wording via ``_translate_sb_errors``,
> stale ``if_match`` 412 wording via
> ``_translate_sb_errors``, dry-run envelope without
> writing, dry-run stale-etag 412, dry-run empty-ref
> upfront guard, dry-run no-match wording, 5xx, timeout).
> 10 new parser-level tests in ``tests/test_tasks.py``
> cover :func:`_apply_checkbox_flip`'s exact output
> (the three state transitions, trailing-newline
> preservation in both shapes, wikilink-alias
> preservation, ``None`` on missing ref, first-occurrence
> splice on multi-match, nested-bullet indent
> preservation) and :func:`_validate_check_task_state`'s
> accept / reject surface. Live e2e in
> ``tests/test_e2e_live_sb.py`` grew a 75-line T30
> round-trip block in the existing live flow: flip
> ``FirstTask`` from ``[ ]`` to ``[x]`` via the live
> path, assert the T23 ack envelope (etag / size /
> timestamps populated), confirm the new state via a
> follow-up ``list_tasks`, run a ``dry_run=True`` flip
> that doesn't land (verified via a third ``list_tasks`
> showing the dry-run was no-op), then roll back via the
> live path so the test leaves the marker page in a
> known state. Carry-forwards: ``tests/test_journal_gate.py``
> ``SB_TOOL_NAMES`` extended from 11 to 12 entries
> (``check_task` joins the set); ``tests/test_http_auth.py``
> sorted ``list_tools()`` shape carries the new tool;
> the ``tests/test_tools_in_memory.py`` module docstring
> bumped "eleven tools" → "twelve tools" with the
> per-tool exception-translation paragraph updated
> (the nine-of-twelve count for ``_translate_sb_errors``
> carry-forwards). Drive-bys: ``README.md`` ``What it
> exposes` bumped from "Eleven tools" to "Twelve tools"
> with the new tool's bullet documenting the wire shape
> and the full error vocabulary; ``§ v1.2 wire-shape
> changes`` "The seven write tools" sentence bumped to
> "The eight write tools" with ``check_task` listed;
> the ``### Dry-run mode (T26)`` section bumped from
> "three read-modify-write tools" to "four"; the Pi-MCP
> wiring paragraph bumped "eleven tools" to "twelve"
> with the new tool in the list. ``CHANGELOG.md``
> Unreleased ``### Added`` section gained a T30 entry
> with the wire shape, the four application-level
> error surfaces, and the ``dry_run=True` envelope
> shape. ``docs/design.md`` Goal line bumped to list
> all twelve tools, ``§ Tools`` prose bumped to
> "Twelve tools, one resource template" with the T29 /
> T30 narrative updated, the Tools table grew a
> ``check_task` row with the wire shape and error
> vocabulary, the ``page_exists` / ``dry_run`
> cross-references in the status-code mapping table
> now mention ``check_task` by name (the 404 row's
> "ToolError("page not found: {name}")" callout
> applies, the 412 row's "precondition failed;
> check if_match/if_none_match" callout applies, the
> 5xx and timeout rows apply). Module docstring of
> ``server.py`` bumped from "ten ``/.fs``-backed tools
> plus one bullet primitive" to "eleven ``/.fs``-backed
> tools plus one bullet primitive" and the
> "T23/T24/T25/T26/T27/T28/T29 done; T30 next" status
> line replaced with "T23/T24/T25/T26/T27/T28/T29/T30
> done; destination reached"; the ``instructions``
> string lists all twelve tool names with the
> ``check_task` description; the ``register_tools`
> docstring bumped "eleven" → "twelve" and "eight of
> the eleven" → "nine of the twelve" for the
> ``_translate_sb_errors` count. Test count: 311 pass
> + 2 skip (was 286 + 2 skip at the start of the T30
> session; +25 net). ``nix flake check` not run (no
> Nix in this env). **The map's destination ("v1.2:
> agent-facing QOL + bullet primitives") is now reached;
> every open ticket on the map is closed.**

## Not yet specified

<!-- dim view of what's coming: things we suspect we'll ticket but
can't yet phrase precisely -->

- **Batched reads/writes (`read_pages(names[])`, `write_pages(…)`)** —
  specifiable when an agent's actual workflow pays for N round trips
  and a measured client (Grok on the web) charges per call.
- **Frontmatter helpers** (`get_frontmatter(name, key?)`,
  `set_frontmatter(name, key, value, if_match?)`,
  `merge_frontmatter(name, updates, if_match?)`) — YAML hand-roll is
  fragile; needs a real YAML dep or a more careful shape. Punt to
  v1.4.
- **Trash layer / soft delete + `restore_page`** — v1's `delete_page`
  is hard delete; the agent composes `read_page → write_page(<backup>)
  → delete_page` itself today. Real undo is a v1.3 candidate.
- **Multi-page operations** (`move_pages(renames[])`,
  `delete_pages(names[])`, `patch_many({name, find, new_string}[])`)
  — same atomicity-caveat pattern as the singular `move_page`.
- **Pagination on `list_pages`** — today the entire space is
  returned in one chunk; a multi-thousand-page space makes the
  response multi-MB. Cursor-paged response is the right answer but
  needs a measured pain point.
- **Locking primitive** (`lock_page(name, ttl_s)` /
  `unlock_page(name)`) — protects an agent's edit window against a
  faster concurrent agent. Real concurrency primitive; different
  design effort.
- **Idempotency keys on writes** — today a retried call does two
  writes (the second fails 412 if the first landed). A header that
  the bridge recognizes ("I already did this") would let it return
  the prior result without re-issuing. Protocol-level concern.
- **Auto-migrate bullets to add wikilink refs** — destructive; the
  user didn't ask for it; explicitly out of scope for v1.2.
  Specifiable when a real "I want to retroactively address my old
  kanban bullets" workflow appears.
- **Token-level / word-level diff** in `diff_pages` — v1.2 ships
  line-based; finer-grained diffing is a v1.3 refinement.
- **Migration guide for clients pinned to v1.1 wire shapes** — T23
  already lands one-line migration notes in `README.md` § v1.2
  wire-shape changes for each of the read / write / list surfaces
  (the `result.text` → `payload["etag"]` swap for write tools, the
  `result.text` → `payload["body"]` swap for the read tool, the
  `text/markdown` → `application/json` MIME-type flip on the
  resource template, the `result["result"]` envelope widening for
  `list_pages`), and the `CHANGELOG.md` Unreleased section lists
  each ticket's breaking-change with the same migration note.
  Considered done unless a downstream consumer surfaces a
  shape that the per-ticket notes don't cover.

## Out of scope

<!-- Work ruled beyond this map's destination. Closed/fog items go in
"Decisions so far" or "Not yet specified" respectively; this section
is for *scope* boundaries. -->

- **`/healthz` endpoint** — operator-facing deploy probe. Was on the
  v1.1 chart's original T14, demoted when v1.1 redrew. v1.2 continues
  to punt; not an agent-facing need.
- **`scopes_supported` in the discovery doc** — one-line
  `AuthSettings(required_scopes=[...])` change. Server-operator
  polish, not agent-facing. Punt.
- **`allowed_origins` env var** — browser-side MCP clients. Punt.
- **`json_response=True` mode** — non-streaming clients. Punt.
- **PR to nixpkgs upgrading `python3Packages.mcp` to v2.x** — same
  punt as the prior maps.
- **Server-pushed notifications / `subscriptions/listen`** — Punt.
- **OAuth 2.1, dynamic-client registration, multi-user.** Locked
  out at T2 of the prior map.
- **Re-deciding design questions locked in `docs/design.md`.** Same
  boundary as the prior maps.
- **Journal-surface write paths.** Read-only carries forward; the
  bullet primitives live on the read side too (`list_tasks` is
  read-only, `check_task` writes through `write_page`, not through
  a journal-specific write path — the existing journal write
  constraint holds).
- **A standalone trash layer / soft delete.** Same boundary as
  the v1.1 map; `delete_page` is hard delete.
- **Markdown-aware patch.** Line-range and string-replace work on
  raw text; the bridge does not parse markdown ASTs.

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

The destination is reached when all eight tickets are closed.

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

### T27. `diff_pages(name, other_name?, other_body?)`

> **Labels**: `wayfinder:task`
> **Type**: AFK
> **Status**: ⬜ open
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

### T29. `list_tasks(page?, prefix?) -> [{ref, line, state, text}]`

> **Labels**: `wayfinder:task`
> **Type**: AFK
> **Status**: ⬜ open
> **Question**: How does the bridge enumerate checkboxes?
>
> **Context**: A SilverBullet "task" is a markdown bullet with a
> `- [ ]` / `- [x]` / `- [X]` checkbox marker. SB's editor uses
> the wikilink on the same bullet (if any, resolved as
> `position` / `linecolumn` / `anchor`) as the task's external
> ref. `list_tasks` walks every checkbox bullet on a page (or
> the whole space when `page` is omitted, filtered by `prefix`
> on the file name) and returns one entry per bullet:
> `{ref, line, state, text}` where:
>
> - `ref` is the wikilink text on the bullet, or `None` if the
>   bullet has no wikilink (in which case the task is not
>   addressable by T30's `check_task`; the agent falls back to
>   `patch_page_lines`).
> - `line` is the 1-indexed line number of the bullet on the page.
> - `state` is one of `" "` (todo), `"x"` (done), `"X"` (done,
>   cancelled — SB's third state).
> - `text` is the rest of the bullet line after the `[ ]` marker
>   (or the full bullet for the journal-style lines that have the
>   marker at the start).
>
> Both `page` and `prefix` are optional; the tool walks the space
> directory when `page` is omitted, matching T11's
> `journal_histogram` / `tag_summary` / T12's
> `pages_touching_topic` shape (same `_iter_md` walker, same
> prefix validation, same hidden-dir skip).
>
> **Done when**: Layer-1 tests cover: page with mix of addressable
> (wikilink) and non-addressable bullets, page with no bullets,
> missing page → `ToolError("page not found: {name}")`, prefix
> filtering, hidden-dir skip, the three states (`" "`, `"x"`,
> `"X"`). Live e2e (T13-shaped) covers
> `/var/lib/silverbullet/Areas/Kanban/Kanban Board - Hobbies.md`
> or equivalent.
>
> **Files when resolved**: `src/mcp_silverbullet/journal.py`
> (new `_list_tasks(space_root, page, prefix)` walker; the
> space-walk variant is gated behind the journal-tools env vars
> from T10 the same way the other journal tools are, while the
> per-page variant is always available because the bridge can
> `read_page` any page it has access to).
>
> **Blocks on**: T10 (journal gate), T11/T12 (the walker
> pattern). **Unblocks**: T30.

---

### T30. `check_task(page, ref, state="done") -> {page, name, etag, ...}`

> **Labels**: `wayfinder:task`
> **Type**: AFK
> **Status**: ⬜ open
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
- **Migration guide for clients pinned to v1.1 wire shapes** — when
  v1.2 lands, the wire shape for every read/write tool changes; a
  short CHANGELOG / migration note for downstream consumers (Grok,
  `mcp` CLI users) is needed. Could be a T23 drive-by or its own
  ticket if the migration gets non-trivial.

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

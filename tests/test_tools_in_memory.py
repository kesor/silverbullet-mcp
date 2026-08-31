"""Layer 1 tests for ``server.py``: in-memory ``Client(mcp)`` with a
mocked SB transport.

We substitute ``httpx.MockTransport`` for a real SilverBullet so the
test suite never needs a running SB. The full HTTP integration matrix
(bridge asgi on a real socket + real auth + discovery doc) lives in
``tests/test_http_auth`` (Layer 2), built on top of this module.

Coverage:

- All fourteen tools — the thirteen ``/.fs``-backed tools
  (``read_page``, ``page_exists``, ``write_page``, ``create_page``,
  ``delete_page``, ``append_to_page``, ``prepend_to_page``,
  ``patch_page_lines``, ``patch_page_replace``, ``move_page``,
  ``list_pages``, ``diff_pages``, ``check_task``) plus the always-on
  bullet enumerator (``list_tasks``) — on the 200 happy path;
  ``write_page`` / ``delete_page`` / ``append_to_page`` /
  ``patch_page_lines`` / ``patch_page_replace`` / ``move_page``
  / ``check_task`` return the T23 write acknowledgement, the
  read tools return the T24 read acknowledgement, ``list_pages``
  returns one envelope per row, ``read_page`` and the resource
  template both surface the markdown body inside the read
  envelope, ``page_exists`` returns ``bool`` (T25),
  ``diff_pages`` returns the unified diff alongside the
  read-side envelopes (T27), ``list_tasks`` returns one entry
  per checkbox bullet (T29). ``append_to_page`` (T19),
  ``patch_page_lines`` (T20), ``patch_page_replace`` (T21),
  ``check_task`` (T30), and ``move_page`` (T22) are the
  read-modify-write / write-then-delete tools.
- ``write_page`` carries the ``if_match`` straight through to
  ``sb_client`` (T3 covers the wire envelope; this test guards the
  MCP-tool-to-SB-client argument path).
- ``list_pages`` filters by prefix client-side.
- Each SB exception maps to ``is_error=True`` with the design doc's
  exact ToolError message: 404 → "page not found: <name>"; 412 →
  "precondition failed; check if_match/if_none_match"; 413 →
  "body too large: limit is 4 MiB"; 5xx → "silverbullet error: <status>";
  timeout → "silverbullet request timed out". Eleven of the
  fourteen tools share the translation through :func:`server._translate_sb_errors`;
  ``page_exists`` (T25) translates 5xx and timeout inline because
  404 is the *answer* (not an error) for the existence question
  — a different exception-translation contract on the ninth tool.
  ``diff_pages`` (T27) threads two ``_translate_sb_errors`` blocks
  (one per read, keyed on the read's target page name) so a 404 on
  either side surfaces with the right page's name in the wording.
  ``check_task`` (T30) wraps the read in
  :func:`server._translate_sb_errors` for the same 404 / 5xx /
  412 wording as the read tool; the write step at the bottom of
  the read-modify-write dance is wrapped separately so a stale
  ``if_match`` on the write surfaces as the unified 412 wording.
  ``check_task`` also has three *application-level* errors
  (``ref not found`` / ``ref matches multiple tasks`` /
  ``state must be one of: done, todo, cancelled``) that fire before
  any SB round trip or between the read and the write — distinct
  from the wire-level ToolError wording and scoped to this tool.
- The resource template returns the same body for the happy path and
  surfaces ``ToolError`` for a missing page (v1 keeps one error shape
  for both surfaces; T4 carry-forward note in the map).

T22 (``move_page``) is the eighth tool: write-then-delete rename
with destination-collision and atomicity-caveat error wording
distinct from the unified 412/404 shapes. T25 (``page_exists``)
adds the ninth tool: a cheap ``bool`` existence check that doesn't
go through :func:`server._translate_sb_errors` because 404 is the
*answer*, not an error. T27 (``diff_pages``) is the tenth tool.
T29 (``list_tasks``) is the eleventh tool: an always-on
per-page checkbox enumerator whose space-walk variant requires
the journal gate. ``list_tasks`` reuses
:func:`server._translate_sb_errors` on its read path, so its
404 / 5xx / timeout wording matches ``read_page``; the
space-walk branch surfaces a ``ToolError(\"list_tasks without
page argument requires the journal surface to be enabled\")``
when the journal gate is off (a different error shape, specific
to the space-walk shape). T30 (``check_task``) is the twelfth
tool: a wikilink-ref-targeted checkbox flip that reads the
page, finds the unique bullet whose wikilink equals ``ref``,
flips the marker (``[ ]`` / ``[x]`` / ``[X]``), and writes the
body back via ``write_page(if_match=<read_etag>)``. Application-
level errors (``ref not found`` / ``ref matches multiple tasks``
/ ``state must be one of: done, todo, cancelled``) fire
without a SB round trip or between the read and the write;
the wire-level read / write steps go through
:func:`server._translate_sb_errors` for the unified 404 / 412
wording.
"""

from __future__ import annotations

import json
import time

import httpx2 as httpx
import pytest
from mcp.client import Client
from mcp.server.mcpserver.exceptions import ToolError

from mcp_silverbullet.sb_client import SBClient
from mcp_silverbullet.server import build_mcp


TOKEN = "test-secret-do-not-use-in-prod"
SB_URL = "http://sb.test"
RESOURCE_URL = "http://bridge.test/mcp"


def _build(handler, *, hydrate_etags: bool = False) -> MCPServer:
    """Build an MCP server whose underlying SB transport is ``handler``.

    ``handler`` is an ``httpx.MockTransport`` callable — it receives
    every request the bridge makes to SB and returns a synthetic
    ``httpx.Response``. The same trick ``tests/test_sb_client.py`` uses
    to test the outbound half; here it lets the in-memory MCP client
    exercise the full tool pipeline without a real SilverBullet.

    ``hydrate_etags`` flips the T28 opt-in: when ``True``, the
    ``list_pages`` tool issues one GET per row to hydrate the etag
    field (the v1 default keeps it off; tests that exercise
    hydration set it explicitly).
    """
    transport = httpx.MockTransport(handler)
    sb = SBClient.__new__(SBClient)
    sb._client = httpx.AsyncClient(
        base_url=SB_URL,
        headers={"Authorization": f"Bearer {TOKEN}"},
        transport=transport,
    )
    return build_mcp(
        sb,
        token=TOKEN,
        resource_url=RESOURCE_URL,
        list_pages_hydrate_etags=hydrate_etags,
    )


def _text(result) -> str:
    """Concatenate the text content of a tool call result."""
    return "".join(
        block.text for block in result.content if getattr(block, "type", None) == "text"
    )


@pytest.fixture(autouse=True)
def _reset_contention_log():
    """Clear the T42 contention counter between tests.

    T42's ``_contention_log`` is a process-global dict (by design —
    a single-user bridge has one in-memory contention view). A
    test that asserts the bare 412 wording ("precondition failed;
    check if_match/if_none_match" with no marker) would otherwise
    start running after enough prior tests have tripped the
    threshold on the same ``name``, and the wire message would
    carry ``[concurrent_edit_hint: true]`` regardless of what
    *this* test did. The fixture clears the dict before every
    test so each one starts with a fresh sliding window.
    """
    from mcp_silverbullet.server import _contention_log

    _contention_log.clear()
    yield
    _contention_log.clear()


# --- read_page ---------------------------------------------------------


@pytest.mark.asyncio
async def test_read_page_returns_ack_envelope_on_200() -> None:
    """``read_page`` returns the T24 acknowledgement envelope.

    v1.1 returned ``str`` (just the markdown body). v1.2 T24 widens
    the return to ``{body, etag, size_bytes, last_modified_ms}``
    so an agent that just read a page knows its etag (for an
    ``if_match`` round-trip on the next write) and its current
    size without a follow-up call. ``name`` is dropped (the caller
    already passed it in) and ``created_ms`` is dropped (reads have
    no create-vs-update distinction to surface); the full meta
    envelope lives on :class:`PageMeta` so a future
    wider-it-still ticket is a one-liner.

    Locks the read-tool wire shape — the ``silverbullet://page/{name}``
    resource template returns the same envelope, so an agent that
    gets the same dict from both surfaces (tool call vs context
    attachment) can treat them identically.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/.fs/index.md"
        return httpx.Response(
            200,
            text="# hello",
            headers={
                "ETag": '"abc123"',
                "X-Last-Modified": "1700000000123",
                "X-Content-Length": "7",
            },
        )

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("read_page", {"name": "index.md"})

    assert result.is_error is False
    # ``size_bytes`` comes from SB's ``X-Content-Length`` response
    # header. ``created_ms`` is intentionally absent (not even
    # ``None``) — T24 drops the field, so the wire payload mirrors
    # that. ``name`` is also dropped (caller already knows it).
    # The SDK surfaces the dict return under ``structured_content``
    # because the handler's return annotation is
    # ``dict[str, object]``.
    assert result.structured_content == {
        "body": "# hello",
        "etag": '"abc123"',
        "size_bytes": 7,
        "last_modified_ms": 1700000000123,
    }


@pytest.mark.asyncio
async def test_read_page_ack_envelope_is_none_when_meta_stripped() -> None:
    """A 200 with no ``X-*`` / ``ETag`` headers → envelope with Nones for meta.

    Mirror of the v1.1 None-ETag handling on the T24 envelope:
    a proxy-stripped response surfaces ``None`` for every optional
    meta field. ``body`` is still the markdown text (always
    populated on a 200).
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="body")

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("read_page", {"name": "index.md"})

    assert result.is_error is False
    assert result.structured_content == {
        "body": "body",
        "etag": None,
        "size_bytes": None,
        "last_modified_ms": None,
    }


@pytest.mark.asyncio
async def test_read_page_404_returns_tool_error_with_design_doc_wording() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="page not found")

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("read_page", {"name": "missing.md"})

    assert result.is_error is True
    # The SDK prefixes the ToolError message with "Error executing
    # tool <name>: "; the design-doc wording "page not found: <name>"
    # is what our handler raises, and the SDK adds the prefix on the
    # wire. Both shapes are stable; assert on the full wire text so a
    # future SDK change to the prefix is caught loudly.
    assert _text(result) == "Error executing tool read_page: page not found: missing.md"


@pytest.mark.asyncio
async def test_read_page_5xx_returns_tool_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="upstream gone")

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("read_page", {"name": "anything.md"})

    assert result.is_error is True
    assert _text(result) == "Error executing tool read_page: silverbullet error: 503"


# --- T39 name normalization --------------------------------------------


@pytest.mark.asyncio
async def test_t39_read_page_normalizes_bare_name_to_md() -> None:
    """``read_page("Foo")`` resolves to ``Foo.md`` on the SB side.

    T39's name-normalization helper threads into every
    ``name``-taking tool: a caller that passes ``"Foo"`` (no
    extension) sees the bridge add ``.md`` before the SB round
    trip, so the agent that asks for ``Foo`` gets the body of
    ``Foo.md`` rather than a 404. The response also carries a
    ``name_resolution`` field so the agent can learn the
    convention for its next call.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        # The bridge should hit ``/.fs/Foo.md``, not ``/.fs/Foo``
        # — that's the whole point of T39.
        assert request.method == "GET"
        assert request.url.path == "/.fs/Foo.md"
        return httpx.Response(
            200,
            text="# body of Foo",
            headers={"X-Content-Length": "14"},
        )

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("read_page", {"name": "Foo"})

    assert result.is_error is False
    sc = result.structured_content or {}
    assert sc["body"] == "# body of Foo"
    # The ``name_resolution`` envelope teaches the agent the
    # convention: ``Foo`` → ``Foo.md``, ``.md`` appended.
    assert sc["name_resolution"] == {
        "requested": "Foo",
        "resolved": "Foo.md",
        "suffix_added": ".md",
    }


@pytest.mark.asyncio
async def test_t39_read_page_canonical_name_is_idempotent() -> None:
    """``read_page("Foo.md")`` is a no-op for the normalization helper.

    The helper is idempotent: a caller that already passes the
    canonical form (``Foo.md``) sees the same SB request and the
    same response envelope (no ``name_resolution`` field — the
    input was already canonical, so there's nothing to teach).
    """
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/.fs/Foo.md"
        return httpx.Response(
            200,
            text="# body of Foo",
            headers={"X-Content-Length": "14"},
        )

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("read_page", {"name": "Foo.md"})

    assert result.is_error is False
    sc = result.structured_content or {}
    assert sc["body"] == "# body of Foo"
    assert "name_resolution" not in sc


@pytest.mark.asyncio
async def test_t39_read_page_preserves_non_md_extension() -> None:
    """A non-md extension (``Foo.txt``) passes through unchanged.

    T39's helper only appends ``.md`` to bare names (no ``.`` in
    the basename). A caller passing ``Foo.txt`` is signalling
    "this is a non-markdown file" — the helper respects that and
    doesn't append ``.md`` (which would produce ``Foo.txt.md``,
    clearly wrong).
    """
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/.fs/Foo.txt"
        return httpx.Response(
            200,
            text="plain text body",
            headers={"X-Content-Length": "16"},
        )

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("read_page", {"name": "Foo.txt"})

    assert result.is_error is False
    sc = result.structured_content or {}
    assert sc["body"] == "plain text body"
    assert "name_resolution" not in sc


@pytest.mark.asyncio
async def test_t39_read_page_strips_whitespace() -> None:
    """Stray whitespace around the name is stripped before normalization.

    The helper strips leading/trailing whitespace *first*, then
    applies the ``.md`` rule. A caller passing ``"  Foo  "``
    resolves to ``Foo.md`` (whitespace stripped, then ``.md``
    appended to the bare basename).
    """
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/.fs/Foo.md"
        return httpx.Response(
            200,
            text="body",
            headers={"X-Content-Length": "4"},
        )

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("read_page", {"name": "  Foo  "})

    assert result.is_error is False
    sc = result.structured_content or {}
    assert sc["body"] == "body"
    assert sc["name_resolution"] == {
        "requested": "  Foo  ",
        "resolved": "Foo.md",
        "suffix_added": ".md",
    }


@pytest.mark.asyncio
async def test_t39_read_page_normalizes_nested_path() -> None:
    """``read_page("Areas/Foo")`` resolves to ``Areas/Foo.md``.

    The helper's extension check is on the *basename* (the
    segment after the last ``/``), so a nested path with a bare
    leaf gets the ``.md`` appended only to the leaf.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/.fs/Areas/Foo.md"
        return httpx.Response(
            200,
            text="body",
            headers={"X-Content-Length": "4"},
        )

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("read_page", {"name": "Areas/Foo"})

    assert result.is_error is False
    sc = result.structured_content or {}
    assert sc["body"] == "body"
    assert sc["name_resolution"] == {
        "requested": "Areas/Foo",
        "resolved": "Areas/Foo.md",
        "suffix_added": ".md",
    }


@pytest.mark.asyncio
async def test_t39_page_exists_normalizes_bare_name() -> None:
    """``page_exists("Foo")`` returns ``True`` when ``Foo.md`` exists.

    ``page_exists`` returns ``bool`` so there is no envelope to
    attach a ``name_resolution`` field to. The agent learns the
    convention by reading the call (``page_exists("Foo")``
    succeeded → the page is at ``Foo.md``).
    """
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/.fs/Foo.md"
        return httpx.Response(200, text="body")

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("page_exists", {"name": "Foo"})

    assert result.is_error is False
    assert result.structured_content == {"result": True}


@pytest.mark.asyncio
async def test_t39_write_page_creates_md_file_for_bare_name() -> None:
    """``write_page("Foo", "hello")`` creates ``Foo.md``, not ``Foo``.

    The write tool's response echoes the canonical name the
    bridge actually used, plus the ``name_resolution`` field so
    the agent can confirm the convention. A caller passing
    ``"Foo"`` sees the response carry ``name="Foo.md"``.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "PUT"
        assert request.url.path == "/.fs/Foo.md"
        return httpx.Response(
            200,
            headers={
                "ETag": '"v1"',
                "X-Created": "1700000000000",
                "X-Last-Modified": "1700000000000",
                "X-Content-Length": "5",
            },
        )

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "write_page", {"name": "Foo", "content": "hello"}
        )

    assert result.is_error is False
    sc = result.structured_content or {}
    # ``name`` echoes the canonical name the bridge used.
    assert sc["name"] == "Foo.md"
    # ``name_resolution`` teaches the agent the convention.
    assert sc["name_resolution"] == {
        "requested": "Foo",
        "resolved": "Foo.md",
        "suffix_added": ".md",
    }


@pytest.mark.asyncio
async def test_t39_write_page_preserves_non_md_extension() -> None:
    """``write_page("Foo.txt", "hello")`` creates ``Foo.txt``.

    The extension-detection rule: ``Foo.txt`` already has a ``.``,
    so the helper doesn't append ``.md``. The agent sees
    ``name="Foo.txt"`` in the response (canonical form, unchanged
    from the input).
    """
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/.fs/Foo.txt"
        return httpx.Response(
            200,
            headers={
                "ETag": '"v1"',
                "X-Created": "1700000000000",
                "X-Last-Modified": "1700000000000",
                "X-Content-Length": "5",
            },
        )

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "write_page", {"name": "Foo.txt", "content": "hello"}
        )

    assert result.is_error is False
    sc = result.structured_content or {}
    assert sc["name"] == "Foo.txt"
    assert "name_resolution" not in sc


@pytest.mark.asyncio
async def test_t39_move_page_normalizes_both_names() -> None:
    """``move_page("Foo", "Bar")`` resolves to ``Foo.md`` → ``Bar.md``.

    Both source and destination normalize independently. The
    source's resolution surfaces on the response envelope; the
    destination's resolution is implicit via the echoed
    ``name`` field (which is the destination's canonical name).
    """
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            assert request.url.path == "/.fs/Foo.md"
            return httpx.Response(200, text="body of Foo")
        if request.method == "PUT":
            assert request.url.path == "/.fs/Bar.md"
            return httpx.Response(
                200,
                headers={
                    "ETag": '"v2"',
                    "X-Created": "1700000000000",
                    "X-Last-Modified": "1700000000000",
                    "X-Content-Length": "9",
                },
            )
        if request.method == "DELETE":
            assert request.url.path == "/.fs/Foo.md"
            return httpx.Response(200, headers={"ETag": '"v2"'})
        raise AssertionError(f"unexpected method {request.method}")

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "move_page", {"name": "Foo", "new_name": "Bar"}
        )

    assert result.is_error is False
    sc = result.structured_content or {}
    # Destination name is the canonical form.
    assert sc["name"] == "Bar.md"
    # Source name resolution surfaces explicitly.
    assert sc["name_resolution"] == {
        "requested": "Foo",
        "resolved": "Foo.md",
        "suffix_added": ".md",
    }


@pytest.mark.asyncio
async def test_t39_move_page_same_name_normalizes_both() -> None:
    """``move_page("Foo", "Foo")`` is a no-op once both sides normalize.

    The same-name short-circuit compares the *resolved* names
    (not the caller's raw inputs), so ``move_page("Foo", "Foo")``
    and ``move_page("Foo.md", "Foo")`` both become no-ops once
    both sides normalize to ``Foo.md``.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        # Only the read is issued (no-op short-circuit).
        assert request.method == "GET"
        assert request.url.path == "/.fs/Foo.md"
        return httpx.Response(
            200,
            text="body of Foo",
            headers={"X-Content-Length": "11"},
        )

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "move_page", {"name": "Foo", "new_name": "Foo.md"}
        )

    assert result.is_error is False
    sc = result.structured_content or {}
    assert sc["name"] == "Foo.md"
    # Source's name_resolution: ``Foo`` → ``Foo.md`` (because
    # that's what the helper did before the same-name comparison).
    assert sc["name_resolution"] == {
        "requested": "Foo",
        "resolved": "Foo.md",
        "suffix_added": ".md",
    }


@pytest.mark.asyncio
async def test_t39_list_tasks_normalizes_page_name() -> None:
    """``list_tasks(page="Areas/Foo")`` reads ``Areas/Foo.md``.

    Per-page form: the page name goes through the normalization
    helper before the SB round trip. Each entry's ``name``
    field carries the canonical name so the agent learns the
    convention by reading the rows.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/.fs/Areas/Foo.md"
        return httpx.Response(
            200,
            text="- [ ] todo with [[Pages/Hobbies]] ref\n",
            headers={"X-Content-Length": "38"},
        )

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "list_tasks", {"page": "Areas/Foo"}
        )

    assert result.is_error is False
    rows = result.structured_content["result"]
    assert len(rows) == 1
    assert rows[0]["name"] == "Areas/Foo.md"


@pytest.mark.asyncio
async def test_t39_check_task_normalizes_page_name() -> None:
    """``check_task(page="Foo", ref="Ref")`` writes to ``Foo.md``.

    The page argument normalizes; the ref argument (a wikilink
    target) does NOT normalize — it's a wikilink target, not a
    page name. The response carries the canonical ``name``
    field and the ``name_resolution`` envelope.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            assert request.url.path == "/.fs/Foo.md"
            return httpx.Response(
                200,
                text="header\n- [ ] task [[Ref]] ref\n",
                headers={"X-Content-Length": "26"},
            )
        if request.method == "PUT":
            assert request.url.path == "/.fs/Foo.md"
            return httpx.Response(
                200,
                headers={
                    "ETag": '"v2"',
                    "X-Created": "1700000000000",
                    "X-Last-Modified": "1700000000000",
                    "X-Content-Length": "26",
                },
            )
        raise AssertionError(f"unexpected method {request.method}")

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "check_task", {"page": "Foo", "ref": "Ref"}
        )

    assert result.is_error is False
    sc = result.structured_content or {}
    assert sc["name"] == "Foo.md"
    assert sc["name_resolution"] == {
        "requested": "Foo",
        "resolved": "Foo.md",
        "suffix_added": ".md",
    }


@pytest.mark.asyncio
async def test_t39_resource_template_normalizes_name() -> None:
    """The ``silverbullet://page/{name}`` resource normalizes too.

    The resource template's path parameter is also normalized:
    reading ``silverbullet://page/Foo`` resolves to
    ``Foo.md`` on the SB side, with the same ``name_resolution``
    feedback on the response envelope.
    """
    import json

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/.fs/Foo.md"
        return httpx.Response(
            200,
            text="# page body",
            headers={"X-Content-Length": "11"},
        )

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.read_resource("silverbullet://page/Foo")

    assert len(result.contents) == 1
    content = result.contents[0]
    assert getattr(content, "mime_type", None) == "application/json"
    payload = json.loads(content.text)
    assert payload["body"] == "# page body"
    assert payload["name_resolution"] == {
        "requested": "Foo",
        "resolved": "Foo.md",
        "suffix_added": ".md",
    }


@pytest.mark.asyncio
async def test_t39_diff_pages_normalizes_both_names() -> None:
    """``diff_pages("Foo", other_name="Bar")`` resolves both sides.

    Each per-page envelope carries its own ``name_resolution``
    field (the first envelope's for ``Foo`` → ``Foo.md``; the
    second envelope's for ``Bar`` → ``Bar.md``).
    """
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/.fs/Foo.md":
            return httpx.Response(200, text="alpha\n")
        if request.url.path == "/.fs/Bar.md":
            return httpx.Response(200, text="alpha\n")
        return httpx.Response(404, text="page not found")

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "diff_pages", {"name": "Foo", "other_name": "Bar"}
        )

    assert result.is_error is False
    payload = result.structured_content or {}
    assert payload["name"]["name_resolution"] == {
        "requested": "Foo",
        "resolved": "Foo.md",
        "suffix_added": ".md",
    }
    assert payload["other"]["name_resolution"] == {
        "requested": "Bar",
        "resolved": "Bar.md",
        "suffix_added": ".md",
    }


# --- page_exists -------------------------------------------------------


@pytest.mark.asyncio
async def test_page_exists_returns_true_on_200() -> None:
    """``page_exists`` returns ``True`` on 200 — the existence is confirmed.

    T25: cheap existence check that costs one ``GET /.fs/{name}``
    round trip without materializing the body. Locks the
    ``bool`` return type and the success path so a future
    refactor that swaps it for a richer envelope doesn't
    silently widen the wire shape.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/.fs/index.md"
        return httpx.Response(200, text="# body")

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("page_exists", {"name": "index.md"})

    assert result.is_error is False
    # ``page_exists`` returns a Python ``bool``; the SDK wraps
    # single-value returns in ``{"result": ...}`` for
    # ``structured_content`` (the same shape as ``list_pages``'s
    # list return). The agent reads ``structured_content["result"]``
    # to get the boolean.
    assert result.structured_content == {"result": True}


@pytest.mark.asyncio
async def test_page_exists_returns_false_on_404() -> None:
    """``page_exists`` returns ``False`` on 404 — not a ``ToolError``.

    T25: 404 is the existence question's "no" answer, *not* an
    error. Compare with :func:`test_read_page_404_returns_tool_error_with_design_doc_wording`
    — same upstream status, different wire shape, different
    tool. The two shapes coexist because the questions are
    different: "give me the body" (read) and "is it there?"
    (exists).
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="page not found")

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("page_exists", {"name": "missing.md"})

    assert result.is_error is False
    assert result.structured_content == {"result": False}


@pytest.mark.asyncio
async def test_page_exists_5xx_returns_tool_error() -> None:
    """5xx surfaces as ``ToolError("silverbullet error: {status}")``.

    T25 deliberately returns an error (not ``False``) on 5xx: "I
    don't know, the server is broken" is not a valid "no". An
    agent that gets ``False`` proceeds with confidence; an agent
    that gets a tool error retries or surfaces the failure. Same
    wording as the other tools so the agent's error-handling
    doesn't have to special-case ``page_exists``.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="upstream gone")

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("page_exists", {"name": "anything.md"})

    assert result.is_error is True
    assert _text(result) == "Error executing tool page_exists: silverbullet error: 503"


@pytest.mark.asyncio
async def test_page_exists_timeout_returns_tool_error() -> None:
    """Timeout surfaces as ``ToolError("silverbullet request timed out")``.

    Locks the timeout wording on the existence tool to match the
    rest of the bridge. ``exists_page`` lets ``httpx.TimeoutException``
    propagate (the SB client doesn't translate timeouts — that's
    the MCP layer's job); the tool handler catches and translates
    it inline, mirroring :func:`_translate_sb_errors` minus the
    404 clause.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("simulated")

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("page_exists", {"name": "anything.md"})

    assert result.is_error is True
    assert _text(result) == "Error executing tool page_exists: silverbullet request timed out"


@pytest.mark.asyncio
async def test_page_exists_412_returns_precondition_tool_error() -> None:
    """A GET that 412s surfaces the unified 412 ``ToolError``.

    ``exists_page`` issues ``GET /.fs/{name}`` and a 412 on a GET
    is unusual (preconditions live on writes), but the SB client
    surfaces any 412 as :class:`PreconditionFailed` and the tool
    handler translates it with the same wording as the other
    tools. Missing this case would mean a proxy / SB
    misconfiguration leaves an unhandled exception, which the SDK
    surfaces as a generic ``MCPError`` without the design-doc
    wording.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(412, text="precondition failed")

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("page_exists", {"name": "index.md"})

    assert result.is_error is True
    assert (
        _text(result)
        == "Error executing tool page_exists: precondition failed; check if_match/if_none_match"
    )


@pytest.mark.asyncio
async def test_page_exists_does_not_materialize_body() -> None:
    """The handler returns a body; ``page_exists`` doesn't surface it.

    Locks the cost-down promise: a body-returning 200 should not
    make the MCP tool's structured content grow a body field. If
    a future refactor swaps ``exists_page`` for ``read_page`` and
    returns the read-side envelope, the wire shape silently widens
    and a caller expecting a ``bool`` breaks.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text="# body that must not leak",
            headers={"ETag": '"abc"', "X-Content-Length": "27"},
        )

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("page_exists", {"name": "index.md"})

    assert result.is_error is False
    # ``bool`` is the wire shape. Nothing else.
    assert result.structured_content == {"result": True}


# --- write_page --------------------------------------------------------


@pytest.mark.asyncio
async def test_write_page_returns_ack_envelope_on_200() -> None:
    """``write_page`` returns the T23 acknowledgement envelope.

    v1.1 returned ``str`` (the new ETag) or ``None` (older SB /
    proxy-stripped). v1.2 T23 widens the return to
    ``{name, etag, size_bytes, last_modified_ms, created_ms}`` so
    the agent knows how big the body is, when it was written, and
    whether the write was a create vs an update without a
    follow-up read.

    Locks the write-tool wire shape — every write tool (the seven
    in ``/.fs``) returns this same envelope per T23.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={
                "ETag": '"abc123"',
                "X-Last-Modified": "1700000000123",
                "X-Created": "1700000000000",
            },
        )

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "write_page",
            {"name": "index.md", "content": "# new body"},
        )

    assert result.is_error is False
    # ``size_bytes`` is the UTF-8 byte count of the just-written body
    # (``# new body`` = 10 bytes), surfaced from the request body.
    # The SDK surfaces the dict return directly under
    # ``structured_content`` (not wrapped in ``{"result": …}`` the
    # way single-value returns are — a dict return IS the
    # structured payload).
    assert result.structured_content == {
        "name": "index.md",
        "etag": '"abc123"',
        "size_bytes": 10,
        "last_modified_ms": 1700000000123,
        "created_ms": 1700000000000,
    }


@pytest.mark.asyncio
async def test_write_page_ack_envelope_is_none_when_meta_stripped() -> None:
    """A 200 with no ``X-*`` / ``ETag`` headers → ack envelope with Nones.

    Mirror of the v1.1 None-ETag handling on the wider T23 envelope:
    a proxy-stripped response surfaces ``None`` for every optional
    meta field (etag / last_modified_ms / created_ms). ``size_bytes``
    is still populated (request-body derivation — the bridge always
    knows how many bytes it wrote).
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200)

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "write_page",
            {"name": "index.md", "content": "# new body"},
        )

    assert result.is_error is False
    assert result.structured_content == {
        "name": "index.md",
        "etag": None,
        "size_bytes": 10,
        "last_modified_ms": None,
        "created_ms": None,
    }


@pytest.mark.asyncio
async def test_write_page_forwards_if_match() -> None:
    """The MCP tool passes ``if_match`` straight to ``sb_client.write_page`.

    T3 covers the wire envelope (X-Source / X-Permission / Content-Type /
    If-Match on the actual HTTP request); this test guards the MCP
    argument path so a future refactor doesn't silently drop the
    parameter or coerce it to the empty string. After the T31b
    post-write verification helper was added, ``write_page`` also
    issues a follow-up ``GET`` (the re-read that compares the
    post-write etag against the caller's ``if_match``); the test
    asserts the PUT envelope is correct regardless of how many
    GETs follow.
    """
    seen_match: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT":
            seen_match.append(request.headers.get("If-Match", ""))
            return httpx.Response(200, headers={"ETag": '"v1"'})
        # Verification GET returns the same etag so the helper's
        # etag compare passes (the page didn't drift between the
        # write and the re-read).
        return httpx.Response(200, headers={"ETag": '"v1"'})

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "write_page",
            {"name": "index.md", "content": "body", "if_match": '"v1"'},
        )

    assert result.is_error is False
    assert seen_match == ['"v1"']


@pytest.mark.asyncio
async def test_write_page_404_returns_tool_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="page not found")

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "write_page", {"name": "missing.md", "content": "body"}
        )

    assert result.is_error is True
    assert _text(result) == "Error executing tool write_page: page not found: missing.md"


@pytest.mark.asyncio
async def test_write_page_412_returns_tool_error_with_design_doc_wording() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(412, text="precondition failed")

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "write_page",
            {"name": "index.md", "content": "body", "if_match": "*"},
        )

    assert result.is_error is True
    assert _text(result) == "Error executing tool write_page: precondition failed; check if_match/if_none_match"


@pytest.mark.asyncio
async def test_write_page_413_returns_tool_error_with_4_mib_wording() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(413, text="body too large")

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "write_page", {"name": "index.md", "content": "x" * 1024}
        )

    assert result.is_error is True
    assert _text(result) == "Error executing tool write_page: body too large: limit is 4 MiB"


@pytest.mark.asyncio
async def test_write_page_5xx_returns_tool_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="bad gateway")

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "write_page", {"name": "index.md", "content": "body"}
        )

    assert result.is_error is True
    assert _text(result) == "Error executing tool write_page: silverbullet error: 502"


# --- delete_page -------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_page_returns_ack_envelope_on_200() -> None:
    """``delete_page`` returns the T23 acknowledgement envelope.

    DELETE doesn't echo ``X-*`` meta per the design doc, so the
    envelope's size / timestamp fields are ``None`` (the honest
    answer — we don't fabricate them). The ETag echoes the deleted
    body's hash so the caller can confirm what was removed. An agent
    that wants the timestamps of what it's about to delete reads
    the page first and threads the etag into ``if_match``.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        assert request.url.path == "/.fs/index.md"
        return httpx.Response(200, headers={"ETag": '"abc123"'})

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("delete_page", {"name": "index.md"})

    assert result.is_error is False
    assert result.structured_content == {
        "name": "index.md",
        "etag": '"abc123"',
        "size_bytes": None,
        "last_modified_ms": None,
        "created_ms": None,
    }


@pytest.mark.asyncio
async def test_delete_page_ack_envelope_when_etag_header_missing() -> None:
    """No ``ETag`` on the response → envelope with etag ``None``.

    Same envelope shape as the success case; only ``etag`` differs.
    Locks the DELETE envelope so a future refactor that drops one of
    the ``None`` fields changes the wire shape loudly.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200)

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("delete_page", {"name": "index.md"})

    assert result.is_error is False
    assert result.structured_content == {
        "name": "index.md",
        "etag": None,
        "size_bytes": None,
        "last_modified_ms": None,
        "created_ms": None,
    }


@pytest.mark.asyncio
async def test_delete_page_forwards_if_match_star() -> None:
    """``if_match="*"`` requires the page to exist; the bridge forwards ``If-Match: *`` verbatim.

    Mirrors :func:`test_write_page_forwards_if_match` for the
    delete path so a future refactor doesn't drop the parameter on
    one tool but not the other.
    """
    seen_match: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_match.append(request.headers.get("If-Match", ""))
        return httpx.Response(200, headers={"ETag": '"v2"'})

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "delete_page",
            {"name": "index.md", "if_match": "*"},
        )

    assert result.is_error is False
    assert seen_match == ["*"]


@pytest.mark.asyncio
async def test_delete_page_404_returns_tool_error_with_design_doc_wording() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="page not found")

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("delete_page", {"name": "missing.md"})

    assert result.is_error is True
    assert _text(result) == "Error executing tool delete_page: page not found: missing.md"


@pytest.mark.asyncio
async def test_delete_page_412_with_if_match_star_returns_tool_error() -> None:
    """``if_match=\"*\"`` on a missing page returns 412 (not 404) at the SB layer.

    SB's semantics: ``\"*\"`` is *must exist*; a missing page is a
    precondition failure, not a not-found. The bridge surfaces the
    unified 412 ToolError wording so callers don't need to distinguish
    “missing” from “stale etag” — they just got refused; they can
    ``read_page`` to figure out which.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(412, text="precondition failed")

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "delete_page", {"name": "missing.md", "if_match": "*"}
        )

    assert result.is_error is True
    assert _text(result) == "Error executing tool delete_page: precondition failed; check if_match/if_none_match"


@pytest.mark.asyncio
async def test_delete_page_412_with_stale_if_match_returns_tool_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(412, text="precondition failed")

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "delete_page",
            {"name": "index.md", "if_match": '"stale"'},
        )

    assert result.is_error is True
    assert _text(result) == "Error executing tool delete_page: precondition failed; check if_match/if_none_match"


@pytest.mark.asyncio
async def test_delete_page_5xx_returns_tool_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="bad gateway")

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("delete_page", {"name": "anything.md"})

    assert result.is_error is True
    assert _text(result) == "Error executing tool delete_page: silverbullet error: 502"


# --- append_to_page ----------------------------------------------------


@pytest.mark.asyncio
async def test_append_to_page_returns_ack_envelope_on_200() -> None:
    """Happy path: existing body + new text → read-modify-write round trip.

    Captures the GET (read) and PUT (write) the tool issues so we
    can assert: (a) the read happened first, (b) the write carries
    the combined body, (c) the tool returns the write's T23 ack
    envelope (not the read's). The envelope's ``size_bytes`` is the
    UTF-8 byte count of the just-written combined body
    (``hello\nworld`` = 11 bytes).
    """
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            calls.append(("GET", request.url.path))
            return httpx.Response(200, text="hello\n")
        # PUT
        calls.append(("PUT", request.url.path))
        assert request.content == b"hello\nworld"
        return httpx.Response(
            200,
            headers={
                "ETag": '"v2"',
                "X-Last-Modified": "1700000000123",
                "X-Created": "1700000000000",
            },
        )

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "append_to_page",
            {"name": "index.md", "text": "world"},
        )

    assert result.is_error is False
    assert result.structured_content == {
        "name": "index.md",
        "etag": '"v2"',
        "size_bytes": 11,  # ``hello\nworld`` = 11 UTF-8 bytes
        "last_modified_ms": 1700000000123,
        "created_ms": 1700000000000,
    }
    # Read first, then write — locks the read-modify-write ordering.
    assert calls == [
        ("GET", "/.fs/index.md"),
        ("PUT", "/.fs/index.md"),
    ]


@pytest.mark.asyncio
async def test_append_to_page_separator_inserted_when_body_lacks_newline() -> None:
    """Body ``"goodbye"`` + ``"hello"`` → ``"goodbye\\nhello"``.

    Locks the separator rule: exactly one newline inserted between
    the two halves when the body doesn't already end in ``\\n``.
    """
    seen_body: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text="goodbye")
        seen_body.append(request.content)
        return httpx.Response(200, headers={"ETag": '"v2"'})

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        await client.call_tool(
            "append_to_page",
            {"name": "index.md", "text": "hello"},
        )

    assert seen_body == [b"goodbye\nhello"]


@pytest.mark.asyncio
async def test_append_to_page_no_extra_separator_when_body_ends_in_newline() -> None:
    """Body ``"hello\\n"`` + ``"world"`` → ``"hello\\nworld"``.

    No double-separator: the body already ends in ``\\n``, so the
    tool only concatenates.
    """
    seen_body: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text="hello\n")
        seen_body.append(request.content)
        return httpx.Response(200, headers={"ETag": '"v2"'})

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        await client.call_tool(
            "append_to_page",
            {"name": "index.md", "text": "world"},
        )

    assert seen_body == [b"hello\nworld"]


@pytest.mark.asyncio
async def test_append_to_page_no_extra_separator_for_multiple_trailing_newlines() -> None:
    """Body ``"hello\\n\\n"`` + ``"world"`` → ``"hello\\n\\nworld"``.

    The rule is ``endswith("\\n")`` (one newline), not
    ``endswith("\\n\\n")`` (two). The trailing-newline case is
    idempotent for callers that want exactly one separator.
    """
    seen_body: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text="hello\n\n")
        seen_body.append(request.content)
        return httpx.Response(200, headers={"ETag": '"v2"'})

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        await client.call_tool(
            "append_to_page",
            {"name": "index.md", "text": "world"},
        )

    assert seen_body == [b"hello\n\nworld"]


@pytest.mark.asyncio
async def test_append_to_page_works_on_empty_body() -> None:
    """Body ``""`` + ``"hello"`` → ``"hello"``.

    The no-body case: no separator. An empty page is a valid
    append target — the first append just becomes the body.
    """
    seen_body: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text="")
        seen_body.append(request.content)
        return httpx.Response(200, headers={"ETag": '"v1"'})

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "append_to_page",
            {"name": "index.md", "text": "hello"},
        )

    assert result.is_error is False
    assert seen_body == [b"hello"]


@pytest.mark.asyncio
async def test_append_to_page_text_with_leading_newline_does_not_double_separator() -> None:
    """Body ``"hello"`` + ``"\\nworld"`` → ``"hello\\n\\nworld"``.

    The tool prepends one ``\\n`` when the body doesn't end in one;
    if the caller also supplies a leading ``\\n`` they get exactly
    one separator between the two halves plus the caller's own
    newline.
    """
    seen_body: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text="hello")
        seen_body.append(request.content)
        return httpx.Response(200, headers={"ETag": '"v2"'})

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        await client.call_tool(
            "append_to_page",
            {"name": "index.md", "text": "\nworld"},
        )

    assert seen_body == [b"hello\n\nworld"]


@pytest.mark.asyncio
async def test_append_to_page_empty_text_returns_tool_error() -> None:
    """``text=""`` is rejected upfront — no read-modify-write round trip.

    An empty append is almost certainly a caller bug. Catching it
    here saves the GET + PUT that would have been a no-op.
    """
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        return httpx.Response(200, text="hello")

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "append_to_page",
            {"name": "index.md", "text": ""},
        )

    assert result.is_error is True
    assert _text(result) == "Error executing tool append_to_page: text must not be empty"
    # No GET, no PUT — the rejection is upfront.
    assert calls == []


@pytest.mark.asyncio
async def test_append_to_page_forwards_if_match_to_write() -> None:
    """``if_match`` is forwarded to the PUT, not the GET.

    The tool's contract: ``if_match`` guards the *write*, not the
    read. A caller who wants "append if no one else has written
    since I last read" passes the etag they got from their last
    ``read_page``; the bridge threads it straight into
    ``If-Match`` on the PUT. The GET carries no precondition.
    T31b added a post-write verification GET (``read_page_meta``)
    that fires after a 200 write; this test asserts the *initial*
    read/write sequence is correct (the verification GET comes
    after the write and is covered by the dedicated T31b tests).
    """
    seen_if_match: list[str] = []
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        seen_if_match.append(request.headers.get("If-Match", ""))
        if request.method == "GET":
            return httpx.Response(200, text="hello")
        return httpx.Response(200, headers={"ETag": '"v2"'})

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        await client.call_tool(
            "append_to_page",
            {"name": "index.md", "text": "world", "if_match": '"v1"'},
        )

    # Initial sequence is read-then-write; the T31b verification
    # GET follows after the write completes (a follow-up GET
    # between the write and any other verification work). The
    # PUT envelope carries the caller's ``If-Match``; the GETs
    # carry no precondition.
    assert calls[0] == "GET"
    assert calls[1] == "PUT"
    assert seen_if_match[0] == ""
    assert seen_if_match[1] == '"v1"'


@pytest.mark.asyncio
async def test_append_to_page_forwards_if_match_star() -> None:
    """``if_match=\"*\"`` requires the page to exist (the write layer checks).

    Coherent with ``write_page``'s ``if_match=\"*\"`` semantics. The
    bridge does not treat this as a *create* — that's
    ``write_page(name, content, if_match=\"*\")``. The read
    happens unconditionally and surfaces a 404 if the page is
    missing (no second 412 from the write path because the page
    isn't there yet).
    """
    seen_if_match: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text="hello")
        seen_if_match.append(request.headers.get("If-Match", ""))
        return httpx.Response(200, headers={"ETag": '"v2"'})

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        await client.call_tool(
            "append_to_page",
            {"name": "index.md", "text": "world", "if_match": "*"},
        )

    assert seen_if_match == ["*"]


@pytest.mark.asyncio
async def test_append_to_page_404_returns_tool_error() -> None:
    """Read raises 404 → ``ToolError("page not found: {name}")``."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="page not found")

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "append_to_page", {"name": "missing.md", "text": "world"}
        )

    assert result.is_error is True
    assert _text(result) == "Error executing tool append_to_page: page not found: missing.md"


@pytest.mark.asyncio
async def test_append_to_page_412_returns_tool_error_with_design_doc_wording() -> None:
    """Stale ``if_match`` on the PUT surfaces the 412 wording.

    The read succeeds; the write fails 412. The bridge maps to the
    unified 412 ``ToolError`` wording so callers don't need to
    distinguish "stale etag" from "if_match='*' on a missing page"
    — they just got refused; they can ``read_page`` to figure out
    which.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text="hello")
        return httpx.Response(412, text="precondition failed")

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "append_to_page",
            {"name": "index.md", "text": "world", "if_match": '"stale"'},
        )

    assert result.is_error is True
    assert _text(result) == "Error executing tool append_to_page: precondition failed; check if_match/if_none_match"


@pytest.mark.asyncio
async def test_append_to_page_413_returns_tool_error_with_4_mib_wording() -> None:
    """Combined body > 4 MiB → ``ToolError("body too large: limit is 4 MiB")``."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text="hello")
        return httpx.Response(413, text="body too large")

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "append_to_page", {"name": "index.md", "text": "world"}
        )

    assert result.is_error is True
    assert _text(result) == "Error executing tool append_to_page: body too large: limit is 4 MiB"


@pytest.mark.asyncio
async def test_append_to_page_5xx_returns_tool_error() -> None:
    """SB 5xx surfaces the ``ToolError("silverbullet error: {status}")`` wording.

    Either the read or the write can 5xx — the unified wording
    applies in both cases because both paths run through
    :func:`_translate_sb_errors`.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text="hello")
        return httpx.Response(502, text="bad gateway")

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "append_to_page", {"name": "index.md", "text": "world"}
        )

    assert result.is_error is True
    assert _text(result) == "Error executing tool append_to_page: silverbullet error: 502"


@pytest.mark.asyncio
async def test_append_to_page_ack_envelope_when_etag_header_missing() -> None:
    """A successful write with no ``ETag`` / ``X-*`` headers → ack envelope with Nones.

    Mirror of :func:`test_write_page_ack_envelope_is_none_when_meta_stripped`
    on the append path. ``size_bytes`` is still populated from the
    request body (the combined body the bridge just wrote:
    ``hello\nworld`` = 11 bytes).
    """
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text="hello\n")
        return httpx.Response(200)

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "append_to_page", {"name": "index.md", "text": "world"}
        )

    assert result.is_error is False
    assert result.structured_content == {
        "name": "index.md",
        "etag": None,
        "size_bytes": 11,
        "last_modified_ms": None,
        "created_ms": None,
    }


# --- dry_run (T26) ----------------------------------------------------
#
# T26 adds a ``dry_run=True`` knob to the three read-modify-write
# tools. The dry-run path: reads the page, computes the patched body
# in-memory, validates ``if_match=<etag>`` against the read's etag
# (since no PUT happens to do it on the server), and returns
# ``{dry_run: True, original: str, patched: str, diff: str}``. No
# PUT is issued; the original page is left untouched. Pre-read input
# validation (empty ``find``, ``text must not be empty``, etc.) still
# fires on dry-run — the caller gets the same specific ToolError the
# live path would surface, not a vague "would have failed".
#
# The next three sections add the per-tool happy-path + error-path
# coverage for ``dry_run=True``. Layer-3 (`sb_client`) coverage is
# implicit: dry-run never reaches ``sb_client.write_page``, so the
# outbound-half tests in ``test_sb_client.py`` already lock the
# "no PUT is issued" contract by absence.


@pytest.mark.asyncio
async def test_append_to_page_dry_run_returns_envelope_without_writing() -> None:
    """``dry_run=True`` on ``append_to_page`` → preview envelope, no PUT.

    Tracks every method the bridge issues so we can assert: (a) the
    read happened (it has to — the tool needs the body to compute
    ``new_body``), (b) no PUT was issued (the whole point of dry-run),
    (c) the envelope contains the original body, the patched body,
    and a unified diff. The diff is a single-line context diff:
    ``--- original\\n+++ patched\\n@@ -1 +1 @@\\n-hello\\n+world\\n``.
    """
    writes: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text="hello\n")
        writes.append(request.content)
        return httpx.Response(200, headers={"ETag": '"v2"'})

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "append_to_page",
            {"name": "index.md", "text": "world", "dry_run": True},
        )

    assert result.is_error is False
    # No PUT issued on the dry-run path — ``writes`` stays empty.
    assert writes == []
    payload = result.structured_content or {}
    assert payload["dry_run"] is True
    assert payload["original"] == "hello\n"
    # Body already ends in ``\\n``; no extra separator is inserted.
    assert payload["patched"] == "hello\nworld"
    # Diff is the unified diff of the change. The context line
    # ``hello`` (unchanged) is preserved, and ``world`` is added.
    # ``difflib.unified_diff`` produces lines without a trailing
    # ``\\n`` (we set ``lineterm=""`` and add ``\\n`` ourselves for
    # line consistency); the diff is therefore ``--- original\\n
    # +++ patched\\n@@ -1,2 +1,2 @@\\n hello\\n-\\n+world\\n``.
    # We assert on the structural pieces rather than the whole
    # string so a future ``difflib`` upgrade that tweaks the
    # header format doesn't break this test.
    diff = payload["diff"]
    assert "--- original\n" in diff
    assert "+++ patched\n" in diff
    assert " hello\n" in diff  # the unchanged context line
    assert "+world\n" in diff  # the added line


@pytest.mark.asyncio
async def test_append_to_page_dry_run_inserts_separator_when_body_lacks_newline() -> None:
    """Dry-run's ``patched`` body has the same separator rule the live path does.

    ``append_to_page`` with body ``"goodbye"`` + text ``"hello"`` →
    live path writes ``"goodbye\\nhello"``. Dry-run must surface the
    same ``"goodbye\\nhello"`` as the patched body so the agent's
    preview matches the post-write reality.
    """
    writes: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text="goodbye")
        writes.append(request.content)
        return httpx.Response(200, headers={"ETag": '"v2"'})

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "append_to_page",
            {"name": "index.md", "text": "hello", "dry_run": True},
        )

    assert writes == []  # no PUT on dry-run
    payload = result.structured_content or {}
    assert payload["original"] == "goodbye"
    assert payload["patched"] == "goodbye\nhello"


@pytest.mark.asyncio
async def test_append_to_page_dry_run_matching_if_match_succeeds() -> None:
    """``if_match=<correct_etag>`` + ``dry_run=True`` → dry-run envelope.

    On the live path, ``if_match`` is forwarded to the PUT; on the
    dry-run path no PUT happens, so the bridge validates the etag
    against the *read's* etag itself. A matching etag passes the
    check and the dry-run envelope is returned.
    """
    writes: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200, text="hello\n", headers={"ETag": '"v1"'}
            )
        writes.append(request.content)
        return httpx.Response(200, headers={"ETag": '"v2"'})

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "append_to_page",
            {
                "name": "index.md",
                "text": "world",
                "if_match": '"v1"',
                "dry_run": True,
            },
        )

    assert result.is_error is False
    assert writes == []
    payload = result.structured_content or {}
    assert payload["dry_run"] is True
    assert payload["patched"] == "hello\nworld"


@pytest.mark.asyncio
async def test_append_to_page_dry_run_stale_if_match_raises_412_tool_error() -> None:
    """``if_match=<stale_etag>`` + ``dry_run=True`` → 412-equivalent ToolError.

    The whole point of dry-run is "would this write succeed?". If
    ``if_match`` is stale, the live path would surface 412 from SB;
    the dry-run path surfaces the same 12-toolerror wording so the
    caller doesn't think a doomed write would have landed.
    """
    writes: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200, text="hello\n", headers={"ETag": '"v1"'}
            )
        writes.append(request.content)
        return httpx.Response(200, headers={"ETag": '"v2"'})

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "append_to_page",
            {
                "name": "index.md",
                "text": "world",
                "if_match": '"stale"',
                "dry_run": True,
            },
        )

    assert result.is_error is True
    assert _text(result) == (
        "Error executing tool append_to_page: "
        "precondition failed; check if_match/if_none_match"
    )
    # Stale etag means no write would have happened anyway — but
    # critically, we still don't issue the PUT. The whole point of
    # dry-run is no writes, full stop.
    assert writes == []


@pytest.mark.asyncio
async def test_append_to_page_dry_run_star_if_match_404s_on_missing_page() -> None:
    """``if_match="*"`` + ``dry_run=True`` on missing page → 404-toolerror.

    ``if_match="*"`` means "require existence" (different shape from
    an etag check). The live path enforces this on the PUT side;
    the dry-run path enforces it the same way the live read does —
    a missing page 404s on the read itself, before any etag check.
    The dry-run never reaches the etag-validate helper for ``"*"``
    (``_validate_if_match_on_read`` short-circuits on ``"*"``), so
    the read's 404 is what surfaces.
    """
    writes: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(404, text="page not found")
        writes.append(request.content)
        return httpx.Response(200, headers={"ETag": '"v2"'})

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "append_to_page",
            {
                "name": "missing.md",
                "text": "world",
                "if_match": "*",
                "dry_run": True,
            },
        )

    assert result.is_error is True
    assert _text(result) == (
        "Error executing tool append_to_page: page not found: missing.md"
    )
    assert writes == []


@pytest.mark.asyncio
async def test_append_to_page_dry_run_empty_text_still_errors() -> None:
    """The ``text must not be empty`` pre-read error fires on dry-run too.

    The pre-read input validation is not optional on the dry-run
    path — a caller that passes ``text=""`` gets the same specific
    ToolError the live path would surface, not a vague "would have
    failed" back. This locks the contract "same preconditions the
    live path would" from the T26 ticket.
    """
    writes: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text="hello\n")
        writes.append(request.content)
        return httpx.Response(200, headers={"ETag": '"v2"'})

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "append_to_page",
            {"name": "index.md", "text": "", "dry_run": True},
        )

    assert result.is_error is True
    assert _text(result) == (
        "Error executing tool append_to_page: text must not be empty"
    )
    # We don't even get to the read step when ``text`` is empty —
    # the pre-read check fires first, so no GET was issued either.
    assert writes == []


@pytest.mark.asyncio
async def test_patch_page_lines_dry_run_returns_envelope_without_writing() -> None:
    """``dry_run=True`` on ``patch_page_lines`` → preview envelope, no PUT.

    The dry-run envelope surfaces the *post-shaping* ``new_body``
    (with the trailing newline re-attached if the body had one), so
    the diff the agent sees is exactly the body that would have been
    written. The body here is ``"a\\nb\\nc\\nd"`` (no trailing
    newline); replacing lines 2-3 with ``"B\\nC"`` patches to
    ``"a\\nB\\nC\\nd"`` (also no trailing newline).
    """
    writes: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text="a\nb\nc\nd")
        writes.append(request.content)
        return httpx.Response(200, headers={"ETag": '"v2"'})

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "patch_page_lines",
            {
                "name": "index.md",
                "start_line": 2,
                "end_line": 3,
                "new_content": "B\nC",
                "dry_run": True,
            },
        )

    assert result.is_error is False
    assert writes == []  # no PUT on dry-run
    payload = result.structured_content or {}
    assert payload["dry_run"] is True
    assert payload["original"] == "a\nb\nc\nd"
    assert payload["patched"] == "a\nB\nC\nd"
    # The diff should show two lines changing (b→B, c→C); the
    # surrounding lines (a, d) are untouched context.
    assert "--- original" in payload["diff"]
    assert "+++ patched" in payload["diff"]
    assert "-b" in payload["diff"]
    assert "-c" in payload["diff"]
    assert "+B" in payload["diff"]
    assert "+C" in payload["diff"]


@pytest.mark.asyncio
async def test_patch_page_lines_dry_run_preserves_trailing_newline() -> None:
    """Dry-run's patched body has the same trailing-newline shape the live path does.

    ``patch_page_lines`` is editor-shaped: a trailing newline on the
    source page is re-attached to the patched body iff the result is
    non-empty. Dry-run must surface the *post-shaping* body (so the
    diff matches what would actually have been written); here the
    source ends in ``\\n``, the patched body is also non-empty, so
    the trailing newline is re-attached.
    """
    writes: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text="a\nb\n")
        writes.append(request.content)
        return httpx.Response(200, headers={"ETag": '"v2"'})

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "patch_page_lines",
            {
                "name": "index.md",
                "start_line": 2,
                "end_line": 2,
                "new_content": "X",
                "dry_run": True,
            },
        )

    assert writes == []
    payload = result.structured_content or {}
    # ``had_trailing_newline=True`` and ``new_body="a\nX"`` (non-empty)
    # → re-attach ``\\n`` → ``"a\\nX\\n"`` is what the live path
    # would write, so it's what dry-run reports.
    assert payload["original"] == "a\nb\n"
    assert payload["patched"] == "a\nX\n"


@pytest.mark.asyncio
async def test_patch_page_lines_dry_run_stale_if_match_raises_412_tool_error() -> None:
    """``if_match=<stale_etag>`` + ``dry_run=True`` → 412-equivalent ToolError."""
    writes: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200, text="a\nb\nc", headers={"ETag": '"v1"'}
            )
        writes.append(request.content)
        return httpx.Response(200, headers={"ETag": '"v2"'})

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "patch_page_lines",
            {
                "name": "index.md",
                "start_line": 1,
                "end_line": 1,
                "new_content": "A",
                "if_match": '"stale"',
                "dry_run": True,
            },
        )

    assert result.is_error is True
    assert _text(result) == (
        "Error executing tool patch_page_lines: "
        "precondition failed; check if_match/if_none_match"
    )
    assert writes == []


@pytest.mark.asyncio
async def test_patch_page_lines_dry_run_out_of_bounds_still_errors() -> None:
    """The post-read out-of-bounds error fires on dry-run too.

    Pre-read errors (``start_line < 1``, inverted range) and post-read
    errors (``end_line > line_count``) both still fire on dry-run —
    the caller gets the same specific ToolError the live path would
    surface, not a vague "would have failed" back. (The pre-read
    errors are covered by the existing live-path tests; this one
    covers the post-read case which depends on the read having
    happened.)
    """
    writes: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text="a\nb")
        writes.append(request.content)
        return httpx.Response(200, headers={"ETag": '"v2"'})

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "patch_page_lines",
            {
                "name": "index.md",
                "start_line": 5,
                "end_line": 6,
                "new_content": "X",
                "dry_run": True,
            },
        )

    assert result.is_error is True
    assert _text(result) == (
        "Error executing tool patch_page_lines: "
        "line range 5..6 out of bounds for page with 2 lines"
    )
    assert writes == []


@pytest.mark.asyncio
async def test_patch_page_replace_dry_run_returns_envelope_without_writing() -> None:
    """``dry_run=True`` on ``patch_page_replace`` → preview envelope, no PUT.

    Body ``"hello world"`` + ``find="world"`` + ``new_string="SB"`` →
    live path writes ``"hello SB"``. Dry-run surfaces the same
    ``"hello SB"`` as the patched body.
    """
    writes: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text="hello world")
        writes.append(request.content)
        return httpx.Response(200, headers={"ETag": '"v2"'})

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "patch_page_replace",
            {
                "name": "index.md",
                "find": "world",
                "new_string": "SB",
                "dry_run": True,
            },
        )

    assert result.is_error is False
    assert writes == []
    payload = result.structured_content or {}
    assert payload["dry_run"] is True
    assert payload["original"] == "hello world"
    assert payload["patched"] == "hello SB"
    # Body has no trailing ``\\n``, so the ``-`` (removed) line and
    # ``+`` (added) line are the only line content of the diff hunk;
    # the diff is ``-hello world\\n+hello SB\\n``.
    diff = payload["diff"]
    assert "-hello world\n" in diff
    assert "+hello SB\n" in diff


@pytest.mark.asyncio
async def test_patch_page_replace_dry_run_replace_all_does_not_write() -> None:
    """``replace_all=True`` + ``dry_run=True`` → all-occurrences preview, no PUT.

    Confirms the ``dry_run`` branch threads the ``replace_all`` knob
    through to the in-memory ``str.replace`` the same way the live
    path does. Body ``"aXbXc"`` + ``find="X"`` + ``new_string="Y"``
    + ``replace_all=True`` → ``"aYbYc"``.
    """
    writes: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text="aXbXc")
        writes.append(request.content)
        return httpx.Response(200, headers={"ETag": '"v2"'})

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "patch_page_replace",
            {
                "name": "index.md",
                "find": "X",
                "new_string": "Y",
                "replace_all": True,
                "dry_run": True,
            },
        )

    assert writes == []
    payload = result.structured_content or {}
    assert payload["patched"] == "aYbYc"


@pytest.mark.asyncio
async def test_patch_page_replace_dry_run_stale_if_match_raises_412_tool_error() -> None:
    """``if_match=<stale_etag>`` + ``dry_run=True`` → 412-equivalent ToolError."""
    writes: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200, text="hello world", headers={"ETag": '"v1"'}
            )
        writes.append(request.content)
        return httpx.Response(200, headers={"ETag": '"v2"'})

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "patch_page_replace",
            {
                "name": "index.md",
                "find": "world",
                "new_string": "SB",
                "if_match": '"stale"',
                "dry_run": True,
            },
        )

    assert result.is_error is True
    assert _text(result) == (
        "Error executing tool patch_page_replace: "
        "precondition failed; check if_match/if_none_match"
    )
    assert writes == []


@pytest.mark.asyncio
async def test_patch_page_replace_dry_run_find_not_found_still_errors() -> None:
    """``find not in body`` + ``dry_run=True`` → ``find not found in body`` ToolError.

    The post-read ``find not in body`` error fires on dry-run too.
    The pre-read ``find must not be empty`` error is covered by the
    existing live-path tests (it's a pre-read check that fires before
    any HTTP traffic; same code path on dry-run).
    """
    writes: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text="hello world")
        writes.append(request.content)
        return httpx.Response(200, headers={"ETag": '"v2"'})

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "patch_page_replace",
            {
                "name": "index.md",
                "find": "absent",
                "new_string": "X",
                "dry_run": True,
            },
        )

    assert result.is_error is True
    assert _text(result) == (
        "Error executing tool patch_page_replace: find not found in body"
    )
    assert writes == []


@pytest.mark.asyncio
async def test_patch_page_replace_dry_run_multiple_matches_default_errors() -> None:
    """``replace_all=False`` + multiple matches + ``dry_run=True`` → same error as live.

    The multiple-match-with-default-replace_all check fires on
    dry-run too — a caller who flips ``replace_all=True`` blindly
    hoping to recover from a typo gets the same loud failure they
    would have gotten with the default. (Confirmed by the existing
    ``test_patch_page_replace_multiple_matches_with_default_errors``
    test on the live path; this just locks the contract on the
    dry-run branch.)
    """
    writes: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text="abc abc abc")
        writes.append(request.content)
        return httpx.Response(200, headers={"ETag": '"v2"'})

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "patch_page_replace",
            {
                "name": "index.md",
                "find": "abc",
                "new_string": "X",
                "dry_run": True,
            },
        )

    assert result.is_error is True
    assert _text(result) == (
        "Error executing tool patch_page_replace: "
        "find matched 3 times; pass replace_all=True or narrow find"
    )
    assert writes == []


@pytest.mark.asyncio
async def test_patch_page_replace_dry_run_no_change_diff_is_empty() -> None:
    """A no-op patch (``new_string == find``) → empty diff in the dry-run envelope.

    An agent that builds a patch and gets back the same body sees
    ``diff=""`` — ``difflib.unified_diff`` returns nothing when the
    two inputs are identical. The agent can use the empty diff as a
    signal that the patch would have been a no-op, without parsing
    the original/patched bodies to find out.
    """
    writes: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text="hello world")
        writes.append(request.content)
        return httpx.Response(200, headers={"ETag": '"v2"'})

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "patch_page_replace",
            {
                "name": "index.md",
                "find": "world",
                "new_string": "world",
                "dry_run": True,
            },
        )

    assert result.is_error is False
    assert writes == []
    payload = result.structured_content or {}
    assert payload["original"] == "hello world"
    assert payload["patched"] == "hello world"
    assert payload["diff"] == ""


# --- patch_page_lines --------------------------------------------------


@pytest.mark.asyncio
async def test_patch_page_lines_returns_etag_on_200() -> None:
    """Happy path: replace middle range, get the new ETag back.

    Locks the read-modify-write shape: the tool reads first, then
    writes the patched body, and returns the write's ETag (not the
    read's). The patched body is asserted on directly so a future
    refactor that drops or duplicates lines fails loudly.
    """
    seen_writes: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text="a\nb\nc\nd")
        seen_writes.append(request.content)
        return httpx.Response(200, headers={"ETag": '"v2"'})

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "patch_page_lines",
            {
                "name": "index.md",
                "start_line": 2,
                "end_line": 3,
                "new_content": "B\nC",
            },
        )

    assert result.is_error is False
    assert result.structured_content == {
        "name": "index.md",
        "etag": '"v2"',
        "size_bytes": 7,  # ``a\nB\nC\nd`` = 7 UTF-8 bytes
        "last_modified_ms": None,
        "created_ms": None,
    }
    # a\nB\nC\nd — lines 2-3 (b, c) replaced by B\nC; the surrounding
    # lines (a, d) are untouched.
    assert seen_writes == [b"a\nB\nC\nd"]


@pytest.mark.asyncio
async def test_patch_page_lines_replaces_first_n_lines() -> None:
    """``start_line=1, end_line=N`` replaces the first N lines."""
    seen_writes: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text="a\nb\nc")
        seen_writes.append(request.content)
        return httpx.Response(200, headers={"ETag": '"v2"'})

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        await client.call_tool(
            "patch_page_lines",
            {
                "name": "index.md",
                "start_line": 1,
                "end_line": 2,
                "new_content": "A\nB",
            },
        )

    assert seen_writes == [b"A\nB\nc"]


@pytest.mark.asyncio
async def test_patch_page_lines_replaces_last_n_lines() -> None:
    """``start_line=N-k, end_line=N`` replaces the last k lines."""
    seen_writes: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text="a\nb\nc\nd")
        seen_writes.append(request.content)
        return httpx.Response(200, headers={"ETag": '"v2"'})

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        await client.call_tool(
            "patch_page_lines",
            {
                "name": "index.md",
                "start_line": 3,
                "end_line": 4,
                "new_content": "C\nD",
            },
        )

    assert seen_writes == [b"a\nb\nC\nD"]


@pytest.mark.asyncio
async def test_patch_page_lines_replaces_entire_body() -> None:
    """``start_line=1, end_line=line_count, new_content=\"x\"`` replaces everything.

    Edge case: the result is just ``\"x\"``, no surrounding lines.
    """
    seen_writes: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text="a\nb\nc")
        seen_writes.append(request.content)
        return httpx.Response(200, headers={"ETag": '"v2"'})

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        await client.call_tool(
            "patch_page_lines",
            {
                "name": "index.md",
                "start_line": 1,
                "end_line": 3,
                "new_content": "x",
            },
        )

    assert seen_writes == [b"x"]


@pytest.mark.asyncio
async def test_patch_page_lines_empty_new_content_deletes_range() -> None:
    """``new_content=\"\"`` deletes the range without adding a replacement.

    Locks the standing-preference rule from the v1.1 map: empty
    ``new_content`` deletes the range, no separate ``delete_lines``
    tool.
    """
    seen_writes: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text="a\nb\nc\nd")
        seen_writes.append(request.content)
        return httpx.Response(200, headers={"ETag": '"v2"'})

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        await client.call_tool(
            "patch_page_lines",
            {
                "name": "index.md",
                "start_line": 2,
                "end_line": 3,
                "new_content": "",
            },
        )

    # ``a\\nd`` — the middle two lines (b, c) were deleted.
    assert seen_writes == [b"a\nd"]


@pytest.mark.asyncio
async def test_patch_page_lines_preserves_trailing_newline() -> None:
    """Body ``\"a\\nb\\n\"`` patched stays ``\"a\\nX\\n\"`` (trailing ``\\n`` preserved).

    Locks the editor-style trailing-newline preservation: the
    read-modify-write round trip doesn't strip the file's final
    newline when it was there to begin with.
    """
    seen_writes: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text="a\nb\n")
        seen_writes.append(request.content)
        return httpx.Response(200, headers={"ETag": '"v2"'})

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        await client.call_tool(
            "patch_page_lines",
            {
                "name": "index.md",
                "start_line": 2,
                "end_line": 2,
                "new_content": "X",
            },
        )

    assert seen_writes == [b"a\nX\n"]


@pytest.mark.asyncio
async def test_patch_page_lines_no_trailing_newline_added_if_body_lacks_one() -> None:
    """Body ``\"a\\nb\"`` patched stays ``\"a\\nX\"`` (no trailing ``\\n`` added).

    Counterpart to the previous test: if the body had no trailing
    newline, the patched result has no trailing newline either. The
    tool doesn't *add* a newline.
    """
    seen_writes: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text="a\nb")
        seen_writes.append(request.content)
        return httpx.Response(200, headers={"ETag": '"v2"'})

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        await client.call_tool(
            "patch_page_lines",
            {
                "name": "index.md",
                "start_line": 2,
                "end_line": 2,
                "new_content": "X",
            },
        )

    assert seen_writes == [b"a\nX"]


@pytest.mark.asyncio
async def test_patch_page_lines_new_content_with_trailing_newline_does_not_double_up() -> None:
    """``new_content=\"X\\n\"`` against body ``\"a\\nb\\n\"`` → ``\"a\\nX\\n\"``.

    The replacement's trailing newline is dropped during the split
    (same as the body's trailing-newline drop) and re-attached at
    the end iff the body had one — so the result has exactly one
    ``\\n`` after ``X``, not two. The replacement's trailing
    newline effectively becomes the line terminator of the last
    replacement line.
    """
    seen_writes: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text="a\nb\n")
        seen_writes.append(request.content)
        return httpx.Response(200, headers={"ETag": '"v2"'})

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        await client.call_tool(
            "patch_page_lines",
            {
                "name": "index.md",
                "start_line": 2,
                "end_line": 2,
                "new_content": "X\n",
            },
        )

    assert seen_writes == [b"a\nX\n"]


@pytest.mark.asyncio
async def test_patch_page_lines_start_line_zero_returns_tool_error() -> None:
    """``start_line=0`` is invalid (lines are 1-indexed) → ``ToolError`` upfront.

    Pre-read validation: the read-modify-write round trip is
    skipped because the input is clearly bad. The error wording
    doesn't carry the page's line count (it isn't known yet).
    """
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        return httpx.Response(200, text="a\nb")

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "patch_page_lines",
            {
                "name": "index.md",
                "start_line": 0,
                "end_line": 1,
                "new_content": "X",
            },
        )

    assert result.is_error is True
    assert _text(result) == "Error executing tool patch_page_lines: start_line must be >= 1, got 0"
    assert calls == []


@pytest.mark.asyncio
async def test_patch_page_lines_negative_start_line_returns_tool_error() -> None:
    """``start_line=-1`` is invalid → ``ToolError`` upfront, no GET/PUT."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        return httpx.Response(200, text="a\nb")

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "patch_page_lines",
            {
                "name": "index.md",
                "start_line": -1,
                "end_line": 1,
                "new_content": "X",
            },
        )

    assert result.is_error is True
    assert _text(result) == "Error executing tool patch_page_lines: start_line must be >= 1, got -1"
    assert calls == []


@pytest.mark.asyncio
async def test_patch_page_lines_end_line_less_than_start_line_returns_tool_error() -> None:
    """``end_line < start_line`` (inverted range) → ``ToolError`` upfront.

    No read needed: an inverted range is always wrong, regardless
    of the page's content. The error carries both endpoints so a
    caller can see exactly which direction they inverted.
    """
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        return httpx.Response(200, text="a\nb\nc")

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "patch_page_lines",
            {
                "name": "index.md",
                "start_line": 3,
                "end_line": 2,
                "new_content": "X",
            },
        )

    assert result.is_error is True
    assert _text(result) == "Error executing tool patch_page_lines: end_line (2) must be >= start_line (3)"
    assert calls == []


@pytest.mark.asyncio
async def test_patch_page_lines_end_line_past_last_line_returns_tool_error() -> None:
    """``end_line > line_count`` → ``ToolError`` with the page's line count.

    Post-read validation: we needed the read to know the page has
    only N lines. The wording matches the v1.1 map's recommended
    ``\"line range {start}..{end} out of bounds for page with {N} lines\"``.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text="a\nb\nc")
        # Should not reach: the write is gated by the bounds check.
        return httpx.Response(200, headers={"ETag": '"v2"'})

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "patch_page_lines",
            {
                "name": "index.md",
                "start_line": 2,
                "end_line": 99,
                "new_content": "X",
            },
        )

    assert result.is_error is True
    assert _text(result) == "Error executing tool patch_page_lines: line range 2..99 out of bounds for page with 3 lines"


@pytest.mark.asyncio
async def test_patch_page_lines_patches_into_an_empty_page() -> None:
    """Body ``\"\"`` + ``patch_page_lines(name, 1, 1, \"x\")`` → ``\"x\"``.

    Empty pages are a valid patch target: the tool splits ``\"\"``
    into ``[]`` (zero lines) but raises out-of-bounds for any
    ``end_line > 0``. Lock that the *only* legal patch on an empty
    page is the no-op range — anything else errors with the empty
    page's line count.
    """
    seen_writes: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text="")
        seen_writes.append(request.content)
        return httpx.Response(200, headers={"ETag": '"v1"'})

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "patch_page_lines",
            {
                "name": "index.md",
                "start_line": 1,
                "end_line": 1,
                "new_content": "x",
            },
        )

    # ``end_line=1`` against ``[]`` is ``end_line > line_count`` —
    # out of bounds. Empty pages can only be patched by replacing
    # the whole thing via ``write_page``.
    assert result.is_error is True
    assert _text(result) == "Error executing tool patch_page_lines: line range 1..1 out of bounds for page with 0 lines"
    assert seen_writes == []


@pytest.mark.asyncio
async def test_patch_page_lines_replaces_only_line_in_single_line_page() -> None:
    """Body ``\"a\"`` + ``patch_page_lines(name, 1, 1, \"X\")`` → ``\"X\"``."""
    seen_writes: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text="a")
        seen_writes.append(request.content)
        return httpx.Response(200, headers={"ETag": '"v2"'})

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        await client.call_tool(
            "patch_page_lines",
            {
                "name": "index.md",
                "start_line": 1,
                "end_line": 1,
                "new_content": "X",
            },
        )

    assert seen_writes == [b"X"]


@pytest.mark.asyncio
async def test_patch_page_lines_handles_universal_newlines_in_body() -> None:
    """The split is on ``\\n`` only, not ``splitlines``.

    A body containing ``\\r\\n`` line endings (which SB doesn't emit
    but a.md stray editor could have left behind) gets one CR character
    at the end of each line — the patch treats the CR as part of the
    line content. The behavior is locked down so a future refactor
    that calls ``splitlines()`` (and silently strips ``\\r``) gets
    caught loudly.
    """
    seen_writes: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text="a\r\nb\r\nc")
        seen_writes.append(request.content)
        return httpx.Response(200, headers={"ETag": '"v2"'})

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        await client.call_tool(
            "patch_page_lines",
            {
                "name": "index.md",
                "start_line": 2,
                "end_line": 2,
                "new_content": "B",
            },
        )

    # Lines are ``"a\r"``, ``"b\r"``, ``"c"`` (split on ``\n``, CR
    # preserved as line content). The join inserts ``\n`` between
    # elements, so replacing ``"b\r"`` with ``"B"`` gives
    # ``"a\r\nB\nc"``. ``str.splitlines()`` would have stripped the
    # ``\r`` and produced ``"a\nB\nc"`` — a quieter divergence.
    assert seen_writes == [b"a\r\nB\nc"]


@pytest.mark.asyncio
async def test_patch_page_lines_forwards_if_match_to_write() -> None:
    """``if_match`` is forwarded to the PUT, not the GET.

    Same contract as ``append_to_page`` (T19): the precondition
    guards the *write*, not the read. A stale etag from the
    caller's last ``read_page`` is the typical use case.
    """
    seen_if_match: list[str] = []
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        seen_if_match.append(request.headers.get("If-Match", ""))
        if request.method == "GET":
            return httpx.Response(200, text="a\nb")
        return httpx.Response(200, headers={"ETag": '"v2"'})

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        await client.call_tool(
            "patch_page_lines",
            {
                "name": "index.md",
                "start_line": 1,
                "end_line": 1,
                "new_content": "A",
                "if_match": '"v1"',
            },
        )

    # Initial sequence is read-then-write; T31b appends a
    # post-write verification GET. The PUT envelope carries the
    # caller's ``If-Match``; the GETs carry no precondition.
    assert calls[0] == "GET"
    assert calls[1] == "PUT"
    assert seen_if_match[0] == ""
    assert seen_if_match[1] == '"v1"'


@pytest.mark.asyncio
async def test_patch_page_lines_stale_if_match_returns_412_tool_error() -> None:
    """Stale ``if_match`` on the PUT → ``ToolError(\"precondition failed; …\")``."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text="a\nb")
        return httpx.Response(412, text="precondition failed")

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "patch_page_lines",
            {
                "name": "index.md",
                "start_line": 1,
                "end_line": 1,
                "new_content": "A",
                "if_match": '"stale"',
            },
        )

    assert result.is_error is True
    assert _text(result) == "Error executing tool patch_page_lines: precondition failed; check if_match/if_none_match"


@pytest.mark.asyncio
async def test_patch_page_lines_404_returns_tool_error() -> None:
    """Read raises 404 → ``ToolError(\"page not found: {name}\")``."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="page not found")

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "patch_page_lines",
            {
                "name": "missing.md",
                "start_line": 1,
                "end_line": 1,
                "new_content": "X",
            },
        )

    assert result.is_error is True
    assert _text(result) == "Error executing tool patch_page_lines: page not found: missing.md"


@pytest.mark.asyncio
async def test_patch_page_lines_413_returns_tool_error_with_4_mib_wording() -> None:
    """Patched body > 4 MiB → ``ToolError(\"body too large: limit is 4 MiB\")``."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text="a\nb")
        return httpx.Response(413, text="body too large")

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "patch_page_lines",
            {
                "name": "index.md",
                "start_line": 1,
                "end_line": 2,
                "new_content": "x" * 1024,
            },
        )

    assert result.is_error is True
    assert _text(result) == "Error executing tool patch_page_lines: body too large: limit is 4 MiB"


@pytest.mark.asyncio
async def test_patch_page_lines_5xx_returns_tool_error() -> None:
    """SB 5xx surfaces the unified ``ToolError(\"silverbullet error: {status}\")`` wording.

    Either the read or the write can 5xx — the unified wording
    applies in both cases because both paths run through
    :func:`_translate_sb_errors`.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text="a\nb")
        return httpx.Response(502, text="bad gateway")

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "patch_page_lines",
            {
                "name": "index.md",
                "start_line": 1,
                "end_line": 1,
                "new_content": "A",
            },
        )

    assert result.is_error is True
    assert _text(result) == "Error executing tool patch_page_lines: silverbullet error: 502"


@pytest.mark.asyncio
async def test_patch_page_lines_ack_envelope_when_etag_header_missing() -> None:
    """A successful write with no ``ETag`` / ``X-*`` headers → ack envelope with Nones.

    Mirror of :func:`test_write_page_ack_envelope_is_none_when_meta_stripped`
    on the patch-lines path. ``size_bytes`` is the just-written
    patched body (``A\nb`` = 3 UTF-8 bytes — line 1 replaced with
    ``A``, line 2 left as ``b``).
    """
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text="a\nb")
        return httpx.Response(200)

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "patch_page_lines",
            {
                "name": "index.md",
                "start_line": 1,
                "end_line": 1,
                "new_content": "A",
            },
        )

    assert result.is_error is False
    assert result.structured_content == {
        "name": "index.md",
        "etag": None,
        "size_bytes": 3,
        "last_modified_ms": None,
        "created_ms": None,
    }


# --- patch_page_replace -------------------------------------------------


@pytest.mark.asyncio
async def test_patch_page_replace_returns_etag_on_200() -> None:
    """Happy path: single literal match → read-modify-write round trip.

    Locks the read-modify-write shape: the tool reads first, then
    writes the replaced body, and returns the write's ETag (not
    the read's). The replaced body is asserted on directly so a
    future refactor that drops or duplicates content fails loudly.
    """
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            calls.append(("GET", request.url.path))
            return httpx.Response(200, text="hello world")
        calls.append(("PUT", request.url.path))
        assert request.content == b"hello SB"
        return httpx.Response(200, headers={"ETag": '"v2"'})

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "patch_page_replace",
            {"name": "index.md", "find": "world", "new_string": "SB"},
        )

    assert result.is_error is False
    assert result.structured_content == {
        "name": "index.md",
        "etag": '"v2"',
        "size_bytes": 8,  # ``hello SB`` = 8 UTF-8 bytes
        "last_modified_ms": None,
        "created_ms": None,
    }
    # Read first, then write — locks the read-modify-write ordering
    # (same shape as ``append_to_page`` and ``patch_page_lines``).
    assert calls == [
        ("GET", "/.fs/index.md"),
        ("PUT", "/.fs/index.md"),
    ]


@pytest.mark.asyncio
async def test_patch_page_replace_default_replace_all_replaces_single_match() -> None:
    """With ``replace_all=False`` (default), a single occurrence is replaced.

    Locks the safe default: a unique substring match is replaced;
    the surrounding body is untouched. Contrast with
    :func:`test_patch_page_replace_multiple_matches_with_default_errors`
    (the multi-match-error path) and
    :func:`test_patch_page_replace_replace_all_true_replaces_every_match`
    (the explicit opt-in).
    """
    seen_writes: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text="hello world")
        seen_writes.append(request.content)
        return httpx.Response(200, headers={"ETag": '"v2"'})

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        await client.call_tool(
            "patch_page_replace",
            {"name": "index.md", "find": "world", "new_string": "SB"},
        )

    assert seen_writes == [b"hello SB"]


@pytest.mark.asyncio
async def test_patch_page_replace_multiple_matches_with_default_errors() -> None:
    """``replace_all=False`` + N > 1 matches → ``ToolError`` upfront.

    The read still happens (the tool can't know the match count
    without it), but the error surfaces *before* the write. The
    wording carries the match count so the caller can decide to
    narrow the find or opt into ``replace_all=True``.
    """
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        return httpx.Response(200, text="foo foo foo")

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "patch_page_replace",
            {"name": "index.md", "find": "foo", "new_string": "FOO"},
        )

    assert result.is_error is True
    assert _text(result) == (
        "Error executing tool patch_page_replace: "
        "find matched 3 times; pass replace_all=True or narrow find"
    )
    # Read happened (we needed it to count matches); no write.
    assert calls == ["GET"]


@pytest.mark.asyncio
async def test_patch_page_replace_multiple_matches_explicit_replace_all_false_errors() -> None:
    """``replace_all=False`` *explicitly* (same wording as default).

    Sanity check that explicit ``False`` doesn't accidentally change
    the behavior — the wording and the no-write outcome must match
    the default case.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="foo foo")

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "patch_page_replace",
            {
                "name": "index.md",
                "find": "foo",
                "new_string": "FOO",
                "replace_all": False,
            },
        )

    assert result.is_error is True
    assert _text(result) == (
        "Error executing tool patch_page_replace: "
        "find matched 2 times; pass replace_all=True or narrow find"
    )


@pytest.mark.asyncio
async def test_patch_page_replace_replace_all_true_replaces_every_match() -> None:
    """``replace_all=True`` → every occurrence is replaced."""
    seen_writes: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text="foo bar foo bar foo")
        seen_writes.append(request.content)
        return httpx.Response(200, headers={"ETag": '"v2"'})

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "patch_page_replace",
            {
                "name": "index.md",
                "find": "foo",
                "new_string": "FOO",
                "replace_all": True,
            },
        )

    assert result.is_error is False
    assert seen_writes == [b"FOO bar FOO bar FOO"]


@pytest.mark.asyncio
async def test_patch_page_replace_find_not_found_returns_tool_error() -> None:
    """Absent ``find`` → ``ToolError`` upfront.

    A silent no-op would mask a typo in the find string and look
    like success. Better to surface the miss loudly so the caller
    re-reads the page and corrects the typo. The read happens (we
    needed it to confirm the find is absent); no write.
    """
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        return httpx.Response(200, text="hello world")

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "patch_page_replace",
            {
                "name": "index.md",
                "find": "missing",
                "new_string": "X",
                "replace_all": True,  # even with all-match opt-in
            },
        )

    assert result.is_error is True
    assert _text(result) == (
        "Error executing tool patch_page_replace: find not found in body"
    )
    assert calls == ["GET"]


@pytest.mark.asyncio
async def test_patch_page_replace_empty_find_returns_tool_error() -> None:
    """``find=\"\"`` is rejected upfront — no read, no write.

    ``\"\".replace`` would match between every character (``\"abc\"``
    becomes ``\"XaXbXcX\"`` for ``new_string=\"X\"``), which is
    almost never what the caller wanted. Surfacing the bug loudly
    upfront saves the round trip and pinpoints the likely caller
    mistake.
    """
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        return httpx.Response(200, text="hello")

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "patch_page_replace",
            {"name": "index.md", "find": "", "new_string": "X"},
        )

    assert result.is_error is True
    assert _text(result) == (
        "Error executing tool patch_page_replace: find must not be empty"
    )
    # No GET, no PUT — the rejection is upfront, mirroring
    # ``append_to_page``'s empty-text guard.
    assert calls == []


@pytest.mark.asyncio
async def test_patch_page_replace_find_with_no_textual_overlap_returns_tool_error() -> None:
    """``replace_all=True`` + absent ``find`` still errors.

    The ``replace_all`` knob is about *how many* to replace, not
    *whether* to find one. The not-found error fires before the
    replace_all branch — so a caller who flips replace_all on
    blindly hoping to recover from a typo gets the same loud
    failure they would have gotten with the default.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="abc")

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "patch_page_replace",
            {
                "name": "index.md",
                "find": "xyz",
                "new_string": "FOO",
                "replace_all": True,
            },
        )

    assert result.is_error is True
    assert _text(result) == (
        "Error executing tool patch_page_replace: find not found in body"
    )


@pytest.mark.asyncio
async def test_patch_page_replace_treats_find_as_literal_not_regex() -> None:
    """``find`` is matched as a literal substring, no regex semantics.

    ``\b`` and ``\\d`` are characters, not regex anchors / character
    classes. A caller who wants regex matches calls ``rg`` or
    Python ``re`` client-side first, then patches with the literal
    result — the bridge doesn't escape for them (escaping
    silently invites \"I forgot to escape\" disasters).
    """
    seen_writes: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text="the \\d placeholder")
        seen_writes.append(request.content)
        return httpx.Response(200, headers={"ETag": '"v2"'})

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        await client.call_tool(
            "patch_page_replace",
            {
                "name": "index.md",
                "find": "\\d",
                "new_string": "X",
                "replace_all": True,
            },
        )

    # Literal ``\\d`` (two characters: backslash + d) gets replaced
    # with ``X`` — NOT regex ``\\d`` (which would mean \"any digit\").
    assert seen_writes == [b"the X placeholder"]


@pytest.mark.asyncio
async def test_patch_page_replace_empty_new_string_deletes_occurrences() -> None:
    """``new_string=\"\"`` deletes the matched substrings.

    No separate ``delete_occurrences`` tool needed — same
    standing-preference rule as ``patch_page_lines``'s empty
    ``new_content`` deletes the range. The result is the body with
    every match removed.
    """
    seen_writes: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text="abcdefg")
        seen_writes.append(request.content)
        return httpx.Response(200, headers={"ETag": '"v2"'})

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        await client.call_tool(
            "patch_page_replace",
            {
                "name": "index.md",
                "find": "cd",
                "new_string": "",
            },
        )

    # ``cd`` is a single match → ``replace_all=False`` succeeds with
    # a deletion. Result is ``abefg``.
    assert seen_writes == [b"abefg"]


@pytest.mark.asyncio
async def test_patch_page_replace_find_spanning_newlines_works() -> None:
    """The ``find`` substring can span line breaks.

    ``str.replace`` is substring-based, not line-based; the body
    is a flat string from the bridge's perspective. A match that
    straddles a ``\\n`` is fine — this differs from
    ``patch_page_lines``'s line-indexed shape intentionally.
    """
    seen_writes: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text="line 1\nline 2\nline 3")
        seen_writes.append(request.content)
        return httpx.Response(200, headers={"ETag": '"v2"'})

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        await client.call_tool(
            "patch_page_replace",
            {
                "name": "index.md",
                "find": "1\nline 2",
                "new_string": "X",
            },
        )

    assert seen_writes == [b"line X\nline 3"]


@pytest.mark.asyncio
async def test_patch_page_replace_forwards_if_match_to_write() -> None:
    """``if_match`` is forwarded to the PUT, not the GET.

    Same contract as ``append_to_page`` and ``patch_page_lines``:
    the precondition guards the *write*, not the read. A stale
    etag from the caller's last ``read_page`` is the typical
    use case.
    """
    seen_if_match: list[str] = []
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        seen_if_match.append(request.headers.get("If-Match", ""))
        if request.method == "GET":
            return httpx.Response(200, text="hello world")
        return httpx.Response(200, headers={"ETag": '"v2"'})

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        await client.call_tool(
            "patch_page_replace",
            {
                "name": "index.md",
                "find": "world",
                "new_string": "SB",
                "if_match": '"v1"',
            },
        )

    # Initial sequence is read-then-write; T31b appends a
    # post-write verification GET. The PUT envelope carries the
    # caller's ``If-Match``; the GETs carry no precondition.
    assert calls[0] == "GET"
    assert calls[1] == "PUT"
    assert seen_if_match[0] == ""
    assert seen_if_match[1] == '"v1"'


@pytest.mark.asyncio
async def test_patch_page_replace_forwards_if_match_star() -> None:
    """``if_match=\"*\"`` requires the page to exist (the write layer checks).

    Coherent with ``append_to_page``'s ``if_match=\"*\"`` semantics:
    the read happens unconditionally, the write carries
    ``If-Match: *``. (The read here is for counting matches; the
    precondition is on the write.)
    """
    seen_if_match: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text="hello world")
        seen_if_match.append(request.headers.get("If-Match", ""))
        return httpx.Response(200, headers={"ETag": '"v2"'})

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        await client.call_tool(
            "patch_page_replace",
            {
                "name": "index.md",
                "find": "world",
                "new_string": "SB",
                "if_match": "*",
            },
        )

    assert seen_if_match == ["*"]


@pytest.mark.asyncio
async def test_patch_page_replace_stale_if_match_returns_412_tool_error() -> None:
    """Stale ``if_match`` on the PUT → ``ToolError(\"precondition failed; …\")``."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text="hello world")
        return httpx.Response(412, text="precondition failed")

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "patch_page_replace",
            {
                "name": "index.md",
                "find": "world",
                "new_string": "SB",
                "if_match": '"stale"',
            },
        )

    assert result.is_error is True
    assert _text(result) == (
        "Error executing tool patch_page_replace: "
        "precondition failed; check if_match/if_none_match"
    )


@pytest.mark.asyncio
async def test_patch_page_replace_404_returns_tool_error() -> None:
    """Read raises 404 → ``ToolError(\"page not found: {name}\")``."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="page not found")

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "patch_page_replace",
            {
                "name": "missing.md",
                "find": "anything",
                "new_string": "X",
            },
        )

    assert result.is_error is True
    assert _text(result) == (
        "Error executing tool patch_page_replace: page not found: missing.md"
    )


@pytest.mark.asyncio
async def test_patch_page_replace_413_returns_tool_error_with_4_mib_wording() -> None:
    """Replaced body > 4 MiB → ``ToolError(\"body too large: limit is 4 MiB\")``."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text="hello world")
        return httpx.Response(413, text="body too large")

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "patch_page_replace",
            {
                "name": "index.md",
                "find": "world",
                "new_string": "X",
            },
        )

    assert result.is_error is True
    assert _text(result) == (
        "Error executing tool patch_page_replace: body too large: limit is 4 MiB"
    )


@pytest.mark.asyncio
async def test_patch_page_replace_5xx_returns_tool_error() -> None:
    """SB 5xx surfaces the unified ``ToolError(\"silverbullet error: {status}\")`` wording.

    Either the read or the write can 5xx — the unified wording
    applies in both cases because both paths run through
    :func:`_translate_sb_errors`.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text="hello world")
        return httpx.Response(502, text="bad gateway")

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "patch_page_replace",
            {
                "name": "index.md",
                "find": "world",
                "new_string": "SB",
            },
        )

    assert result.is_error is True
    assert _text(result) == (
        "Error executing tool patch_page_replace: silverbullet error: 502"
    )


@pytest.mark.asyncio
async def test_patch_page_replace_ack_envelope_when_etag_header_missing() -> None:
    """A successful write with no ``ETag`` / ``X-*`` headers → ack envelope with Nones.

    Mirror of :func:`test_write_page_ack_envelope_is_none_when_meta_stripped`
    on the patch-replace path. ``size_bytes`` is the just-written
    replaced body (``hello SB`` = 8 UTF-8 bytes).
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text="hello world")
        return httpx.Response(200)

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "patch_page_replace",
            {
                "name": "index.md",
                "find": "world",
                "new_string": "SB",
            },
        )

    assert result.is_error is False
    assert result.structured_content == {
        "name": "index.md",
        "etag": None,
        "size_bytes": 8,
        "last_modified_ms": None,
        "created_ms": None,
    }


# --- list_pages --------------------------------------------------------


@pytest.mark.asyncio
async def test_list_pages_returns_file_metas_on_200() -> None:
    """``list_pages`` widens to the T23 envelope family (T28).

    v1 returned ``list[{name, etag}]`` (the minimal subset);
    v1.2 T28 widens to the same envelope family the read and
    write tools use — ``list[{name, etag, size_bytes,
    last_modified_ms, created_ms}]``. The list payload carries
    ``size`` from SB; ``created`` / ``lastModified`` are
    absent from the test payload so the corresponding fields
    surface as ``None`` per the defensive-parsing contract
    (``_parse_int_header`` returns ``None`` for missing /
    malformed header values, same as the read / write paths).

    Hydration is **off by default** — the new field tuple
    includes the etag field, but on this SB build the list
    payload doesn't carry one (the v1 map's T10 decision
    documented this), so every row's ``etag`` is ``None``
    unless the operator opts in to
    ``MCP_SILVERBULLET_LIST_PAGES_HYDRATE_ETAGS=1``.
    """
    payload = [
        {"name": "index.md", "etag": '"a"', "size": 12},
        {"name": "page-2.md", "etag": None, "size": 7},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/.fs"
        return httpx.Response(200, content=__import__("json").dumps(payload).encode())

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("list_pages", {})

    assert result.is_error is False
    # The tool returns ``list[dict]``; the SDK wraps that as
    # ``{"result": [...]}`` per the SDK's ``RootModel`` shape
    # on ``list`` returns. Asserting on the structured payload
    # avoids string-encoding fragility (the values include
    # epoch-ms integers and ``None`` placeholders that wouldn't
    # round-trip through JSON text cleanly).
    assert result.structured_content == {
        "result": [
            {
                "name": "index.md",
                "etag": '"a"',
                "size_bytes": 12,
                "last_modified_ms": None,
                "created_ms": None,
            },
            {
                "name": "page-2.md",
                "etag": None,
                "size_bytes": 7,
                "last_modified_ms": None,
                "created_ms": None,
            },
        ]
    }


@pytest.mark.asyncio
async def test_list_pages_filters_by_prefix() -> None:
    payload = [
        {"name": "index.md", "etag": None, "size": 1},
        {"name": "journal/2026-01-01.md", "etag": None, "size": 2},
        {"name": "journal/2026-01-02.md", "etag": None, "size": 3},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=__import__("json").dumps(payload).encode())

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("list_pages", {"prefix": "journal/"})

    assert result.is_error is False
    # T28 widened each row to the full envelope family; only
    # rows matching ``prefix`` are returned. ``etag`` is
    # ``None`` here because hydration is off by default
    # (the SB list payload omits the field on this build; an
    # operator who needs the etag opts in to
    # ``MCP_SILVERBULLET_LIST_PAGES_HYDRATE_ETAGS=1``, see
    # the test_list_pages_hydrates_etags_* tests below).
    assert result.structured_content == {
        "result": [
            {
                "name": "journal/2026-01-01.md",
                "etag": None,
                "size_bytes": 2,
                "last_modified_ms": None,
                "created_ms": None,
            },
            {
                "name": "journal/2026-01-02.md",
                "etag": None,
                "size_bytes": 3,
                "last_modified_ms": None,
                "created_ms": None,
            },
        ]
    }


# --- list_pages substring filter (T37) --------------------------------


@pytest.mark.asyncio
async def test_list_pages_contains_filter() -> None:
    """T37: ``contains=`` does substring matching against page name.

    The bug reporter's `kesor_list_pages` symptom ("prefix silently
    ignored") was a v1-pre-T10 regression on the ``prefix=`` side;
    T37 widens the surface so an operator who reads "filtered by
    prefix" as substring has an explicit knob. ``prefix`` keeps
    ``startswith`` semantics (unchanged for v1 / v1.1 / v1.2 / v1.3
    callers); ``contains`` is the new substring narrowing. Both
    compose as AND when both are set.
    """
    payload = [
        {"name": "index.md", "etag": None, "size": 1},
        {"name": "journal/2026-01-01.md", "etag": None, "size": 2},
        {"name": "trade-journal-2026-q1.md", "etag": None, "size": 3},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=__import__("json").dumps(payload).encode())

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("list_pages", {"contains": "journal"})

    assert result.is_error is False
    # ``journal`` substring appears in both ``journal/…`` and
    # ``trade-journal-…``; ``index.md`` is filtered out.
    assert result.structured_content == {
        "result": [
            {
                "name": "journal/2026-01-01.md",
                "etag": None,
                "size_bytes": 2,
                "last_modified_ms": None,
                "created_ms": None,
            },
            {
                "name": "trade-journal-2026-q1.md",
                "etag": None,
                "size_bytes": 3,
                "last_modified_ms": None,
                "created_ms": None,
            },
        ]
    }


@pytest.mark.asyncio
async def test_list_pages_prefix_and_contains_compose() -> None:
    """T37: ``prefix=`` + ``contains=`` compose as AND.

    Both set means the row must satisfy both criteria — a tighter
    set than either alone, never a wider one. The
    ``trade-journal-…`` row matches ``contains="journal"`` but
    fails ``prefix="journal/"``; only the rows under the
    ``journal/`` folder that also contain the substring survive.
    """
    payload = [
        {"name": "index.md", "etag": None, "size": 1},
        {"name": "journal/2026-01-01.md", "etag": None, "size": 2},
        {"name": "journal/2026-01-02.md", "etag": None, "size": 3},
        {"name": "trade-journal-2026-q1.md", "etag": None, "size": 4},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=__import__("json").dumps(payload).encode())

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "list_pages",
            {"prefix": "journal/", "contains": "2026"},
        )

    assert result.is_error is False
    assert result.structured_content == {
        "result": [
            {
                "name": "journal/2026-01-01.md",
                "etag": None,
                "size_bytes": 2,
                "last_modified_ms": None,
                "created_ms": None,
            },
            {
                "name": "journal/2026-01-02.md",
                "etag": None,
                "size_bytes": 3,
                "last_modified_ms": None,
                "created_ms": None,
            },
        ]
    }


@pytest.mark.asyncio
async def test_list_pages_contains_empty_is_full_list() -> None:
    """T37: ``contains=""`` returns the full listing (v1 behavior).

    Empty string is the no-op sentinel for the ``contains`` filter
    — same as ``prefix=""`` already shipped. A caller that passes
    ``{}`` (no filters at all) gets the v1 surface byte-for-byte:
    the full listing, no narrowing, no extra cost. This pins the
    surface so a future refactor that flips the empty-check
    semantics doesn't silently drop rows.
    """
    payload = [
        {"name": "index.md", "etag": None, "size": 1},
        {"name": "journal/2026-01-01.md", "etag": None, "size": 2},
        {"name": "trade-journal-2026-q1.md", "etag": None, "size": 3},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=__import__("json").dumps(payload).encode())

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("list_pages", {"contains": ""})

    assert result.is_error is False
    # Full listing — the empty ``contains`` filter is a no-op.
    assert result.structured_content == {
        "result": [
            {
                "name": "index.md",
                "etag": None,
                "size_bytes": 1,
                "last_modified_ms": None,
                "created_ms": None,
            },
            {
                "name": "journal/2026-01-01.md",
                "etag": None,
                "size_bytes": 2,
                "last_modified_ms": None,
                "created_ms": None,
            },
            {
                "name": "trade-journal-2026-q1.md",
                "etag": None,
                "size_bytes": 3,
                "last_modified_ms": None,
                "created_ms": None,
            },
        ]
    }


@pytest.mark.asyncio
async def test_list_pages_contains_runs_before_hydration() -> None:
    """T37: ``contains`` filter runs before per-row hydration, same
    as ``prefix``.

    The order (filter → hydrate) is locked by T28's
    ``test_list_pages_hydration_runs_after_prefix_filter`` test
    so that a narrow filter reduces the per-page round-trip
    count; T37 inherits the same ordering because the new
    filter is mechanically identical (one-line substring
    narrowing). A future refactor that moves hydration before
    either filter would surface as wasted SB load rather than
    a silent regression.
    """
    list_payload = [
        {"name": "index.md", "size": 1},
        {"name": "journal/2026-01-01.md", "size": 2},
        {"name": "trade-journal-2026-q1.md", "size": 3},
    ]

    per_page_etags = {
        "index.md": '"hydrated-index"',
        "journal/2026-01-01.md": '"hydrated-journal-2026-01-01"',
        "trade-journal-2026-q1.md": '"hydrated-trade-journal-2026-q1"',
    }

    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        if request.url.path == "/.fs":
            return httpx.Response(
                200, content=__import__("json").dumps(list_payload).encode()
            )
        # Per-page GET — match against the path after ``/.fs/``.
        name = request.url.path[len("/.fs/"):]
        return httpx.Response(
            200,
            text="body",
            headers={"ETag": per_page_etags[name]},
        )

    # Hydration on, narrow ``contains`` filter — only the journal
    # rows should be visited for etag-hydration (the index
    # row's etag is irrelevant because the filter discards it
    # before the hydration walker runs).
    server = _build(handler, hydrate_etags=True)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("list_pages", {"contains": "journal"})

    assert result.is_error is False
    # Exactly one ``GET /.fs`` (the list call) + two
    # ``GET /.fs/{name}`` (hydration for the two journal
    # rows). The index row's hydration is skipped because the
    # ``contains`` filter discards it before the walker runs.
    # Filter-then-hydrate ordering is locked here.
    assert seen == [
        ("GET", "/.fs"),
        ("GET", "/.fs/journal/2026-01-01.md"),
        ("GET", "/.fs/trade-journal-2026-q1.md"),
    ]
    assert result.structured_content == {
        "result": [
            {
                "name": "journal/2026-01-01.md",
                "etag": '"hydrated-journal-2026-01-01"',
                "size_bytes": 2,
                "last_modified_ms": None,
                "created_ms": None,
            },
            {
                "name": "trade-journal-2026-q1.md",
                "etag": '"hydrated-trade-journal-2026-q1"',
                "size_bytes": 3,
                "last_modified_ms": None,
                "created_ms": None,
            },
        ]
    }


@pytest.mark.asyncio
async def test_list_pages_5xx_returns_tool_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("list_pages", {})

    assert result.is_error is True
    assert _text(result) == "Error executing tool list_pages: silverbullet error: 500"


# --- list_pages hydration (T28 opt-in) --------------------------------


@pytest.mark.asyncio
async def test_list_pages_hydration_off_by_default_keeps_etag_none() -> None:
    """T28 default: hydration off → ``etag=None`` for every row.

    The operator who doesn't need ``if_match`` round-trips from a
    list call shouldn't pay the N+1 cost. The list payload's
    ``etag`` field is ``None`` on this SB build; with
    ``hydrate_etags=False`` (the default), the tool returns the
    list as-is. A future SB build that emits ``etag`` in the list
    payload would short-circuit the hydration walker and surface
    the real etag; this test pins down the *current* SB-build
    behaviour so a regression that flips the default on
    silently is caught loudly.
    """
    payload = [
        {"name": "index.md", "size": 1},
        {"name": "page-2.md", "size": 2},
    ]

    def recording(request: httpx.Request) -> httpx.Response:
        # Only the ``GET /.fs`` list call should happen — no per-
        # page GETs. The test would fail if hydration ran by
        # default because the handler would see more requests
        # than just the list call.
        return httpx.Response(
            200,
            content=__import__("json").dumps(payload).encode(),
        )

    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        return recording(request)

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("list_pages", {})

    assert result.is_error is False
    # Exactly one ``GET /.fs`` (the list call). No
    # ``GET /.fs/{name}`` hydration calls.
    assert seen == [("GET", "/.fs")]
    # All rows have ``etag=None`` (the SB payload omits the
    # field; hydration is off so we don't fetch it).
    assert result.structured_content == {
        "result": [
            {
                "name": "index.md",
                "etag": None,
                "size_bytes": 1,
                "last_modified_ms": None,
                "created_ms": None,
            },
            {
                "name": "page-2.md",
                "etag": None,
                "size_bytes": 2,
                "last_modified_ms": None,
                "created_ms": None,
            },
        ]
    }


@pytest.mark.asyncio
async def test_list_pages_hydrates_etags_when_opted_in() -> None:
    """Opt-in hydration: per-page GET replaces ``etag=None``.

    With ``MCP_SILVERBULLET_LIST_PAGES_HYDRATE_ETAGS=1`` (modelled
    here as ``hydrate_etags=True`` on the bridge factory), the
    ``list_pages`` tool issues one ``GET /.fs/{name}`` per row
    whose list-payload etag is ``None``. The hydrated etag
    comes from the response's ``ETag`` header; the other
    meta fields (``size_bytes`` / ``last_modified_ms`` /
    ``created_ms``) come from the list payload directly and
    don't change on hydration (T28 keeps the list-side values
    authoritative for those fields; hydration is etag-only).

    Pins down the request ordering: ``GET /.fs`` first, then
    one ``GET /.fs/{name}`` per row, in the order the list
    returned them. A future refactor that fan-outs with
    ``asyncio.gather`` would change the request count to N
    (not N+1) and the ordering to non-deterministic; this
    test catches that loudly.
    """
    list_payload = [
        {"name": "index.md", "size": 1, "created": 1700000000000},
        {"name": "page-2.md", "size": 2, "created": 1700000001000},
    ]

    # Per-page ETag values keyed by page name. The hydration
    # walker issues one GET per page and surfaces each row's
    # ``ETag`` as the etag field.
    per_page_etags = {
        "index.md": '"hydrated-a"',
        "page-2.md": '"hydrated-b"',
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/.fs":
            return httpx.Response(
                200, content=__import__("json").dumps(list_payload).encode()
            )
        # Per-page GET — match against the path after ``/.fs/``.
        name = request.url.path[len("/.fs/"):]
        etag = per_page_etags[name]
        return httpx.Response(
            200,
            text="body bytes we don't want",
            headers={
                "ETag": etag,
                "X-Content-Length": "999",
                "X-Last-Modified": "1700000009999",
            },
        )

    seen: list[tuple[str, str]] = []

    def recording(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        return handler(request)

    server = _build(recording, hydrate_etags=True)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("list_pages", {})

    assert result.is_error is False
    # The list call is first, followed by one per-page GET
    # per row, in order. ``asyncio.gather`` would parallelise
    # the per-page GETs but produce the same set of paths; we
    # assert on the set, not the order beyond the initial
    # list call, to avoid flakiness on a parallel future
    # implementation.
    paths = [path for _, path in seen]
    assert paths[0] == "/.fs"
    assert sorted(paths[1:]) == ["/.fs/index.md", "/.fs/page-2.md"]
    # Each row's etag is the hydrated value; the size /
    # timestamps come from the list payload (not the per-page
    # GET — hydration is etag-only).
    assert result.structured_content == {
        "result": [
            {
                "name": "index.md",
                "etag": '"hydrated-a"',
                "size_bytes": 1,
                "last_modified_ms": 1700000009999,
                "created_ms": 1700000000000,
            },
            {
                "name": "page-2.md",
                "etag": '"hydrated-b"',
                "size_bytes": 2,
                "last_modified_ms": 1700000009999,
                "created_ms": 1700000001000,
            },
        ]
    }


@pytest.mark.asyncio
async def test_list_pages_hydration_skips_rows_with_list_payload_etag() -> None:
    """Future-proofing: a row whose list payload carries ``etag`` is not re-fetched.

    SB builds that emit ``etag`` in the list payload (a future
    fix for the gap T28 documents) shouldn't pay the per-page
    round trip — the hydration walker checks
    ``meta.etag is not None`` and skips the GET. The current
    SB build always emits ``None`` here, so this test simulates
    the future shape to lock down the short-circuit.
    """
    list_payload = [
        # Row 1: etag already present — hydration skips it.
        {"name": "index.md", "etag": '"from-list"', "size": 1},
        # Row 2: etag is null — hydration fires.
        {"name": "page-2.md", "etag": None, "size": 2},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/.fs":
            return httpx.Response(
                200, content=__import__("json").dumps(list_payload).encode()
            )
        # Only ``page-2`` should reach here.
        assert request.url.path == "/.fs/page-2.md"
        return httpx.Response(
            200,
            text="body",
            headers={"ETag": '"hydrated-page-2"'},
        )

    server = _build(handler, hydrate_etags=True)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("list_pages", {})

    assert result.is_error is False
    assert result.structured_content == {
        "result": [
            {
                "name": "index.md",
                "etag": '"from-list"',
                "size_bytes": 1,
                "last_modified_ms": None,
                "created_ms": None,
            },
            {
                "name": "page-2.md",
                "etag": '"hydrated-page-2"',
                "size_bytes": 2,
                "last_modified_ms": None,
                "created_ms": None,
            },
        ]
    }


@pytest.mark.asyncio
async def test_list_pages_hydration_survives_per_page_404() -> None:
    """One page 404'ing during hydration doesn't fail the whole list.

    ``read_page_meta_safe`` swallows the per-page 404 and
    returns ``None``; the row stays in the result with
    ``etag=None``. The agent sees a list with one row whose
    etag is unknown rather than an exception that aborts the
    whole call. The other rows' etags are still hydrated.
    """
    list_payload = [
        {"name": "index.md", "size": 1},
        {"name": "deleted.md", "size": 2},  # 404 on hydrate
        {"name": "page-3.md", "size": 3},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/.fs":
            return httpx.Response(
                200, content=__import__("json").dumps(list_payload).encode()
            )
        if request.url.path == "/.fs/deleted.md":
            # Page was deleted between the list and hydrate.
            return httpx.Response(404, text="page not found")
        # The other two pages hydrate cleanly.
        name = request.url.path[len("/.fs/"):]
        return httpx.Response(
            200, text="body", headers={"ETag": f'"{name}-etag"'}
        )

    server = _build(handler, hydrate_etags=True)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("list_pages", {})

    assert result.is_error is False
    assert result.structured_content == {
        "result": [
            {
                "name": "index.md",
                "etag": '"index.md-etag"',
                "size_bytes": 1,
                "last_modified_ms": None,
                "created_ms": None,
            },
            {
                "name": "deleted.md",
                "etag": None,
                "size_bytes": 2,
                "last_modified_ms": None,
                "created_ms": None,
            },
            {
                "name": "page-3.md",
                "etag": '"page-3.md-etag"',
                "size_bytes": 3,
                "last_modified_ms": None,
                "created_ms": None,
            },
        ]
    }


@pytest.mark.asyncio
async def test_list_pages_hydration_survives_per_page_5xx() -> None:
    """A transient 5xx on one hydration GET doesn't fail the list.

    Same resilience contract as the 404 case: a single page's
    SB hiccup leaves the row's etag as ``None`` rather than
    aborting the whole list. The agent can retry the list
    later if it wants the missing etag.
    """
    list_payload = [
        {"name": "index.md", "size": 1},
        {"name": "flaky.md", "size": 2},  # 503 on hydrate
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/.fs":
            return httpx.Response(
                200, content=__import__("json").dumps(list_payload).encode()
            )
        if request.url.path == "/.fs/flaky.md":
            return httpx.Response(503, text="upstream gone")
        return httpx.Response(
            200, text="body", headers={"ETag": '"index-etag"'}
        )

    server = _build(handler, hydrate_etags=True)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("list_pages", {})

    assert result.is_error is False
    rows = result.structured_content["result"]
    assert rows[0]["etag"] == '"index-etag"'
    assert rows[1]["etag"] is None
    assert rows[1]["name"] == "flaky.md"


@pytest.mark.asyncio
async def test_list_pages_hydration_survives_per_page_timeout() -> None:
    """A timeout on one hydration GET doesn't fail the list.

    Same resilience contract: ``read_page_meta_safe`` swallows
    ``httpx.TimeoutException`` and returns ``None``. The row
    stays in the result with ``etag=None``. Without this, a
    slow page in a 200-page space would turn a 1-second
    ``list_pages`` into a 30-second hung call when SB is under
    load.
    """
    list_payload = [
        {"name": "index.md", "size": 1},
        {"name": "slow.md", "size": 2},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/.fs":
            return httpx.Response(
                200, content=__import__("json").dumps(list_payload).encode()
            )
        if request.url.path == "/.fs/slow.md":
            raise httpx.ReadTimeout("simulated")
        return httpx.Response(
            200, text="body", headers={"ETag": '"index-etag"'}
        )

    server = _build(handler, hydrate_etags=True)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("list_pages", {})

    assert result.is_error is False
    rows = result.structured_content["result"]
    assert rows[0]["etag"] == '"index-etag"'
    assert rows[1]["etag"] is None
    assert rows[1]["name"] == "slow.md"


@pytest.mark.asyncio
async def test_list_pages_hydration_runs_after_prefix_filter() -> None:
    """Hydration fires only on the post-prefix-filter rows.

    ``prefix`` filtering happens *after* the list call (the
    bridge has to list the whole space and filter in Python
    per the v1 design — server-side Space Lua search is out of
    scope per T4 of the prior map). When hydration is on, the
    walker should only hit the rows that survived the prefix
    filter — visiting a row the agent's about to discard
    anyway would be wasted round trips. Locks down the
    ordering: filter first, hydrate second.
    """
    list_payload = [
        {"name": "index.md", "size": 1},
        {"name": "journal/2026-01-01.md", "size": 2},
        {"name": "journal/2026-01-02.md", "size": 3},
    ]

    seen_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_paths.append(request.url.path)
        if request.url.path == "/.fs":
            return httpx.Response(
                200, content=__import__("json").dumps(list_payload).encode()
            )
        name = request.url.path[len("/.fs/"):]
        return httpx.Response(
            200, text="body", headers={"ETag": f'"{name}-etag"'}
        )

    server = _build(handler, hydrate_etags=True)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "list_pages", {"prefix": "journal/"}
        )

    assert result.is_error is False
    # No GET for ``/.fs/index`` — hydration skipped it because
    # the prefix filter discarded it. Only the two ``journal/``
    # rows reached the hydration walker.
    assert "/.fs/index.md" not in seen_paths
    assert "/.fs/journal/2026-01-01.md" in seen_paths
    assert "/.fs/journal/2026-01-02.md" in seen_paths
    # The result is just the filtered rows.
    names = [r["name"] for r in result.structured_content["result"]]
    assert names == ["journal/2026-01-01.md", "journal/2026-01-02.md"]


# --- move_page --------------------------------------------------------


@pytest.mark.asyncio
async def test_move_page_returns_new_etag_on_200() -> None:
    """Happy path: read → write new → delete old, return new etag.

    Locks the write-then-delete ordering (the ticket's atomicity
    choice — write first so a partial-failure case leaves the body
    at the new name rather than losing it). The new page's ETag
    is returned; the source's ETag is discarded.
    """
    calls: list[tuple[str, str, bytes]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.content if request.method != "GET" else b""
        calls.append((request.method, request.url.path, body))
        if request.method == "GET":
            assert request.url.path == "/.fs/old.md"
            return httpx.Response(200, text="the body\n")
        if request.method == "PUT":
            assert request.url.path == "/.fs/new.md"
            # If-None-Match: * on the destination write — move is
            # rename, never silently overwrite.
            assert request.headers.get("If-None-Match") == "*"
            assert request.content == b"the body\n"
            return httpx.Response(200, headers={"ETag": '"new-etag"'})
        # DELETE
        assert request.url.path == "/.fs/old.md"
        return httpx.Response(200, headers={"ETag": '"old-etag"'})

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "move_page", {"name": "old.md", "new_name": "new.md"}
        )

    assert result.is_error is False
    assert result.structured_content == {
        "name": "new.md",  # destination, not source — per T23
        "etag": '"new-etag"',
        "size_bytes": 9,  # ``the body\n`` = 9 UTF-8 bytes
        "last_modified_ms": None,
        "created_ms": None,
    }
    # Order: GET source → PUT destination (with If-None-Match) →
    # DELETE source. The write happens before the delete so a
    # partial-failure leaves the body at the new name.
    assert [(m, p) for m, p, _ in calls] == [
        ("GET", "/.fs/old.md"),
        ("PUT", "/.fs/new.md"),
        ("DELETE", "/.fs/old.md"),
    ]
    # The body written to the destination matches what was read.
    assert calls[1][2] == b"the body\n"


@pytest.mark.asyncio
async def test_move_page_same_name_is_no_op() -> None:
    """``name == new_name`` short-circuits: read for existence, no PUT/DELETE.

    Avoids the read-write-delete cycle (and the spurious 412 that
    the dance would invite — we'd just write a fresh body to the
    same path, making the etag from the read stale for the delete
    that follows). T23: the same-name no-op now returns the page's
    full acknowledgement envelope (etags, size, timestamps) since
    the underlying ``read_page`` already populates it — no
    extra round trip, just a one-line unwrap on the existing
    read response.
    """
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        return httpx.Response(
            200,
            text="body",
            headers={"ETag": '"self-etag"', "X-Content-Length": "4"},
        )

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "move_page", {"name": "self.md", "new_name": "self.md"}
        )

    assert result.is_error is False
    # Only the existence check ran.
    assert calls == [("GET", "/.fs/self.md")]
    # The same-name no-op returns the page's read-side ack — the
    # caller gets the size / etag / timestamps without an extra
    # round trip.
    assert result.structured_content == {
        "name": "self.md",
        "etag": '"self-etag"',
        "size_bytes": 4,
        "last_modified_ms": None,
        "created_ms": None,
    }


@pytest.mark.asyncio
async def test_move_page_same_name_missing_returns_404_tool_error() -> None:
    """Same-name short-circuit on a missing page surfaces 404 wording.

    The short-circuit still needs to verify existence — silently
    succeeding on a missing page would mask a caller typo.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="page not found")

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "move_page", {"name": "ghost.md", "new_name": "ghost.md"}
        )

    assert result.is_error is True
    assert _text(result) == "Error executing tool move_page: page not found: ghost.md"


@pytest.mark.asyncio
async def test_move_page_forwards_if_match_to_delete() -> None:
    """``if_match`` is threaded into the source DELETE, not the GET.

    Mirrors the append/patch contract: ``if_match`` guards the
    *write* side (here the source delete — the etag from the read
    is the natural anchor). The read carries no precondition; the
    destination PUT carries ``If-None-Match: *`` (move is rename,
    not merge). T31b adds a post-delete verification GET that
    short-circuits to 404 (the source is gone) — see the dedicated
    T31b tests for that path.
    """
    seen_if_match: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_if_match.append(request.headers.get("If-Match", ""))
        if request.method == "GET":
            return httpx.Response(200, text="body")
        if request.method == "PUT":
            return httpx.Response(200, headers={"ETag": '"new"'})
        return httpx.Response(200, headers={"ETag": '"old"'})

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        await client.call_tool(
            "move_page",
            {"name": "old.md", "new_name": "new.md", "if_match": '"v1"'},
        )

    # Initial sequence: GET (no precondition), PUT (If-None-Match,
    # so empty ``If-Match``), DELETE (caller's ``if_match``).
    # T31b's post-delete verification GET (which carries no
    # ``If-Match`` either) follows and is covered by the T31b
    # tests; this test pins the initial-sequence envelope.
    assert seen_if_match[:3] == ["", "", '"v1"']


@pytest.mark.asyncio
async def test_move_page_404_on_read_returns_tool_error() -> None:
    """Source missing on the read surfaces the standard 404 wording."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="page not found")

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "move_page", {"name": "missing.md", "new_name": "new.md"}
        )

    assert result.is_error is True
    assert _text(result) == "Error executing tool move_page: page not found: missing.md"


@pytest.mark.asyncio
async def test_move_page_destination_exists_returns_destination_collision_error() -> None:
    """Destination PUT 412 (from ``If-None-Match: *``) → collision wording.

    Distinct from the unified 412 wording: the caller asked to move
    a *different* page to ``new_name`` and the destination already
    exists. Saying "page not found: {name}" would be wrong;
    saying just "precondition failed" would be ambiguous (source
    or destination?). The destination-collision message names the
    destination and refuses, period.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text="body")
        # PUT to /new — already exists.
        return httpx.Response(412, text="precondition failed")

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "move_page", {"name": "old.md", "new_name": "new.md"}
        )

    assert result.is_error is True
    assert (
        _text(result)
        == "Error executing tool move_page: destination page already exists: new.md; refusing to overwrite"
    )


@pytest.mark.asyncio
async def test_move_page_delete_412_returns_atomicity_caveat_error() -> None:
    """Source DELETE 412 after a successful write → atomicity-caveat wording.

    The destination already has the body; the source couldn't be
    deleted (probably because its etag went stale — concurrent
    edit between the read and the delete). Both names now point
    at a page; the caller needs to clean up. The error names both
    names and tells the caller to delete the duplicate.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text="body")
        if request.method == "PUT":
            return httpx.Response(200, headers={"ETag": '"new"'})
        # DELETE fails 412 — source etag stale.
        return httpx.Response(412, text="precondition failed")

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "move_page", {"name": "old.md", "new_name": "new.md"}
        )

    assert result.is_error is True
    assert (
        _text(result)
        == "Error executing tool move_page: moved body to new.md but failed to delete old.md: precondition failed; check if_match/if_none_match; both now exist"
    )


@pytest.mark.asyncio
async def test_move_page_delete_404_surfaces_atomicity_message_not_generic_404() -> None:
    """Source DELETE 404 (deleted between read and delete) → atomicity message.

    The body is at ``new_name`` already — that's what the caller
    wanted. The source going missing during cleanup is a feature
    (someone else deleted it for us), not a bug. The generic
    "page not found: old.md" wording would be misleading.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text="body")
        if request.method == "PUT":
            return httpx.Response(200, headers={"ETag": '"new"'})
        return httpx.Response(404, text="page not found")

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "move_page", {"name": "old.md", "new_name": "new.md"}
        )

    assert result.is_error is True
    assert (
        _text(result)
        == "Error executing tool move_page: moved body to new.md but old.md was already deleted before the cleanup step"
    )


@pytest.mark.asyncio
async def test_move_page_delete_5xx_returns_atomicity_caveat_error() -> None:
    """Source DELETE 5xx after a successful write → atomicity-caveat wording.

    Server-side failure during cleanup. The body is at both names;
    the caller needs to know to retry the delete (or recover
    manually).
    """
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text="body")
        if request.method == "PUT":
            return httpx.Response(200, headers={"ETag": '"new"'})
        return httpx.Response(502, text="bad gateway")

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "move_page", {"name": "old.md", "new_name": "new.md"}
        )

    assert result.is_error is True
    assert (
        _text(result)
        == "Error executing tool move_page: moved body to new.md but failed to delete old.md: silverbullet error: 502; both now exist"
    )


@pytest.mark.asyncio
async def test_move_page_delete_timeout_returns_atomicity_caveat_error() -> None:
    """Source DELETE timeout after a successful write → atomicity-caveat wording.

    Timeouts during cleanup leave the body at both names; the
    caller retries or recovers manually.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text="body")
        if request.method == "PUT":
            return httpx.Response(200, headers={"ETag": '"new"'})
        raise httpx.ReadTimeout("simulated")

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "move_page", {"name": "old.md", "new_name": "new.md"}
        )

    assert result.is_error is True
    assert (
        _text(result)
        == "Error executing tool move_page: moved body to new.md but failed to delete old.md: silverbullet request timed out; both now exist"
    )


@pytest.mark.asyncio
async def test_move_page_destination_write_413_returns_tool_error() -> None:
    """Destination write 413 → standard body-too-large wording.

    The body read from the source exceeded SB's 4 MiB limit when
    re-PUT to the destination. The source isn't deleted (the
    PUT failed before the DELETE ran), so the body still lives at
    ``name``. Standard wording — the partial-failure shape is
    "destination write failed", not "source cleanup failed", so
    the atomicity message doesn't apply.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text="body")
        return httpx.Response(413, text="body too large")

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "move_page", {"name": "old.md", "new_name": "new.md"}
        )

    assert result.is_error is True
    assert (
        _text(result)
        == "Error executing tool move_page: body too large: limit is 4 MiB"
    )


@pytest.mark.asyncio
async def test_move_page_read_5xx_returns_tool_error() -> None:
    """Source read 5xx surfaces the unified 5xx wording."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="upstream gone")

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "move_page", {"name": "old.md", "new_name": "new.md"}
        )

    assert result.is_error is True
    assert _text(result) == "Error executing tool move_page: silverbullet error: 503"


@pytest.mark.asyncio
async def test_move_page_ack_envelope_when_new_etag_header_missing() -> None:
    """Destination write 200 with no ETag / ``X-*`` headers → ack envelope with Nones.

    Mirror of the same shape on the other write tools. The wire
    payload is the destination's T23 ack envelope (``name=new``
    because the destination is what we wrote and the caller wants
    to know about). ``size_bytes`` is still populated from the
    request body (``body`` = 4 UTF-8 bytes).
    """
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text="body")
        if request.method == "PUT":
            return httpx.Response(200)
        return httpx.Response(200, headers={"ETag": '"old"'})

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "move_page", {"name": "old.md", "new_name": "new.md"}
        )

    assert result.is_error is False
    assert result.structured_content == {
        "name": "new.md",  # destination
        "etag": None,
        "size_bytes": 4,
        "last_modified_ms": None,
        "created_ms": None,
    }


@pytest.mark.asyncio
async def test_move_page_does_not_delete_on_destination_collision() -> None:
    """When the destination write fails, the source is not deleted.

    The atomicity story is "write first, then delete". If the
    write fails (412 from ``If-None-Match: *``), the source
    DELETE never runs — the source is untouched. This pins down
    the ordering so a future refactor that moved the DELETE
    before the PUT (or ran them concurrently) doesn't silently
    lose data on a destination collision.
    """
    methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        if request.method == "GET":
            return httpx.Response(200, text="body")
        # Destination collision.
        return httpx.Response(412, text="precondition failed")

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "move_page", {"name": "old.md", "new_name": "new.md"}
        )

    assert result.is_error is True
    # Read, then failed PUT — no DELETE.
    assert methods == ["GET", "PUT"]


# --- resource template -------------------------------------------------


@pytest.mark.asyncio
async def test_resource_template_returns_ack_envelope() -> None:
    """``silverbullet://page/{name}`` returns the T24 ack envelope.

    v1.1 returned a raw markdown string (``text/markdown``). v1.2
    T24 widens the resource to return the same
    ``{body, etag, size_bytes, last_modified_ms}`` envelope as the
    read tool, JSON-serialized into the SDK's resource text field;
    the MIME type is ``application/json`` because the value is a
    structured object, not raw markdown. Callers parse
    ``content.text`` as JSON to read the envelope (or
    ``json.loads(content.text)["body"]`` to grab the markdown).

    Locks the resource wire shape; the read tool's envelope is
    tested separately above.
    """
    import json

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/.fs/index.md"
        return httpx.Response(
            200,
            text="# page body",
            headers={
                "ETag": '"abc123"',
                "X-Last-Modified": "1700000000123",
                "X-Content-Length": "11",
            },
        )

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.read_resource("silverbullet://page/index.md")

    assert len(result.contents) == 1
    content = result.contents[0]
    # MIME type flipped from ``text/markdown`` (v1.1 raw body) to
    # ``application/json`` (v1.2 structured envelope). The body
    # lives inside the JSON, not as raw text.
    assert getattr(content, "mime_type", None) == "application/json"
    assert json.loads(content.text) == {
        "body": "# page body",
        "etag": '"abc123"',
        "size_bytes": 11,
        "last_modified_ms": 1700000000123,
    }


@pytest.mark.asyncio
async def test_resource_template_404_raises_resource_not_found_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="page not found")

    from mcp.shared.exceptions import MCPError

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        with pytest.raises(MCPError) as exc_info:
            await client.read_resource("silverbullet://page/missing")

    # The SDK maps ``ResourceNotFoundError`` to ``-32602 invalid
    # params`` per SEP-2164; the message text is what Grok surfaces.
    assert "page not found: missing.md" in str(exc_info.value)


# --- timeout -----------------------------------------------------------


@pytest.mark.asyncio
async def test_read_page_timeout_returns_tool_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("simulated")

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("read_page", {"name": "anything.md"})

    assert result.is_error is True
    assert _text(result) == "Error executing tool read_page: silverbullet request timed out"


# --- diff_pages (T27) -------------------------------------------------
#
# T27 adds ``diff_pages(name, other_name?, other_body?)`` — a line-
# based unified diff between two pages or a page and a literal
# string. The wire shape is ``{diff, name, other?}``: ``diff`` is the
# unified diff (empty string when the two bodies are identical),
# ``name`` is the read-side envelope for the first page (with the
# page's name included so the shape is parallel with ``other``),
# and ``other`` is the same envelope for the second page when
# ``other_name`` was given. The tool returns 404-equivalent ToolError
# when either page is missing; passing neither or both of
# ``other_name`` / ``other_body`` is rejected upfront so the read
# round trip isn't wasted on a confused input shape.


@pytest.mark.asyncio
async def test_diff_pages_page_vs_page_returns_unified_diff() -> None:
    """Two pages, both exist: diff with both envelopes surfaced.

    Locks the wire shape and the basic diff content. The first
    page's name appears in the ``fromfile`` header of the diff
    (``--- first``), the second's in the ``tofile`` header
    (``+++ second``); the agent reading the diff back can map
    ``-`` lines to ``first`` and ``+`` lines to ``second`` without
    ambiguity. Both envelopes carry ``name`` (parallel shape;
    the second envelope's name is what the agent needs to know
    which page the right side came from).
    """
    pages = {
        "first.md": "alpha\nbeta\ngamma\n",
        "second.md": "alpha\nBETA\ngamma\n",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        name = request.url.path.removeprefix("/.fs/")
        if name not in pages:
            return httpx.Response(404, text="page not found")
        return httpx.Response(
            200,
            text=pages[name],
            headers={
                "ETag": f'"{name}-etag"',
                "X-Content-Length": str(len(pages[name].encode("utf-8"))),
            },
        )

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "diff_pages",
            {"name": "first.md", "other_name": "second.md"},
        )

    assert result.is_error is False
    payload = result.structured_content or {}
    # Per-page envelopes include ``name`` so the shape is parallel
    # (the second envelope's ``name`` is what the agent reads to
    # know which page the diff's right side came from).
    assert payload["name"] == {
        "name": "first.md",
        "body": "alpha\nbeta\ngamma\n",
        "etag": '"first.md-etag"',
        "size_bytes": 17,
        "last_modified_ms": None,
    }
    assert payload["other"] == {
        "name": "second.md",
        "body": "alpha\nBETA\ngamma\n",
        "etag": '"second.md-etag"',
        "size_bytes": 17,
        "last_modified_ms": None,
    }
    # ``difflib.unified_diff`` normals ``---``/``+++`` headers with
    # the file names; assert on the structural pieces so a future
    # ``difflib`` upgrade that tweaks the header format doesn't
    # break this test.
    diff = payload["diff"]
    assert "--- first.md\n" in diff
    assert "+++ second.md\n" in diff
    assert "-beta\n" in diff
    assert "+BETA\n" in diff
    assert " alpha\n" in diff  # unchanged context line
    assert " gamma\n" in diff  # unchanged context line


@pytest.mark.asyncio
async def test_diff_pages_page_vs_literal_string() -> None:
    """``other_body`` (no second GET): only the first-page envelope surfaces.

    The literal-string variant issues one read, not two — the
    second body comes from the caller's argument, not from SB.
    The wire shape drops the ``other`` envelope (it's ``None``,
    not an empty envelope with ``name=<literal>``, because the
    literal never had a name to begin with). The diff's ``tofile``
    header uses ``<literal>`` so the agent can tell which side
    came from where.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/.fs/only.md"
        return httpx.Response(
            200,
            text="hello\nworld\n",
            headers={"ETag": '"only-etag"'},
        )

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "diff_pages",
            {"name": "only.md", "other_body": "hello\nWORLD\n"},
        )

    assert result.is_error is False
    payload = result.structured_content or {}
    assert payload["name"]["body"] == "hello\nworld\n"
    # ``other`` is ``None`` for the literal-string case — there
    # is no second page envelope to surface.
    assert payload["other"] is None
    diff = payload["diff"]
    assert "--- only.md\n" in diff
    assert "+++ <literal>\n" in diff
    assert "-world\n" in diff
    assert "+WORLD\n" in diff


@pytest.mark.asyncio
async def test_diff_pages_identical_bodies_yields_empty_diff() -> None:
    """Identical inputs → ``diff=""`` (no-op patch, no ``-``/``+`` lines).

    ``difflib.unified_diff`` returns an empty iterator when the two
    inputs are equal, so the diff string is empty. The agent
    reading the diff back reads ``""`` as "would have changed
    nothing" without parsing the ``original`` / ``patched``
    bodies — same shape as :func:`_dry_run_payload`'s no-op case.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        return httpx.Response(200, text="same\nlines\n", headers={"ETag": '"e"'})

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "diff_pages",
            {"name": "a.md", "other_name": "b"},
        )

    assert result.is_error is False
    payload = result.structured_content or {}
    assert payload["diff"] == ""
    # Both envelopes still surface (the caller might want the
    # etag from either side for an ``if_match`` round-trip after
    # the diff, even though the bodies are identical).
    assert payload["name"]["body"] == "same\nlines\n"
    assert payload["other"]["body"] == "same\nlines\n"


@pytest.mark.asyncio
async def test_diff_pages_neither_other_name_nor_other_body_errors() -> None:
    """Passing neither flag → ToolError upfront, no read round trip.

    Locks the input-validation contract: a caller that confused
    the two flags (or forgot to pass either) gets the same
    specific ``ToolError`` the live path would surface, not a
    confusing partial-diff result. The error fires before any
    GET to SB so the call costs nothing.
    """
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        return httpx.Response(200, text="body")

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("diff_pages", {"name": "any.md"})

    assert result.is_error is True
    assert (
        _text(result)
        == "Error executing tool diff_pages: pass exactly one of other_name or other_body"
    )
    # No GET was issued — the input validation fires before any
    # read round trip.
    assert calls == []


@pytest.mark.asyncio
async def test_diff_pages_both_other_name_and_other_body_errors() -> None:
    """Passing both flags → same ToolError upfront, no read round trip.

    Same contract as the neither-flag case: ambiguous input is
    rejected before the read round trip so the agent sees the
    specific ToolError without paying for a wasted GET. The error
    wording is shared with the neither-flag case because the
    caller's bug is the same shape — they confused the two flags
    — and the agent gets one diagnostic to learn from either way.
    """
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        return httpx.Response(200, text="body")

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "diff_pages",
            {
                "name": "any.md",
                "other_name": "second.md",
                "other_body": "literal",
            },
        )

    assert result.is_error is True
    assert (
        _text(result)
        == "Error executing tool diff_pages: pass exactly one of other_name or other_body"
    )
    assert calls == []


@pytest.mark.asyncio
async def test_diff_pages_first_page_missing_returns_404_with_name_in_wording() -> None:
    """First page missing → ``ToolError(\"page not found: <name>\")`` for the first page.

    The agent passed two names (``name`` + ``other_name``); when
    the first one is missing the wording surfaces the first name
    so the agent can tell which side failed. The same shape as
    the read tool's 404 wording — :func:`_translate_sb_errors`
    threads the same exception translation into ``diff_pages``.
    No second read is attempted because the first one's 404
    short-circuits the handler.
    """
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.url.path == "/.fs/missing-first.md":
            return httpx.Response(404, text="page not found")
        return httpx.Response(200, text="present")

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "diff_pages",
            {"name": "missing-first.md", "other_name": "present"},
        )

    assert result.is_error is True
    assert (
        _text(result)
        == "Error executing tool diff_pages: page not found: missing-first.md"
    )
    # Only one GET was issued — the first-page 404 short-circuits
    # before the second read.
    assert calls == [("GET", "/.fs/missing-first.md")]


@pytest.mark.asyncio
async def test_diff_pages_second_page_missing_returns_404_with_other_name_in_wording() -> None:
    """Other page missing → ``ToolError(\"page not found: <other_name>\")``.

    The error wording carries the *other* page's name when the
    second read 404s, so the agent can distinguish "first page
    missing" from "second page missing" without inspecting the
    call. Same design-doc ``ToolError`` shape as the read tool's
    404 handling.
    """
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.url.path == "/.fs/missing-other.md":
            return httpx.Response(404, text="page not found")
        return httpx.Response(200, text="present", headers={"ETag": '"p"'})

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "diff_pages",
            {"name": "present.md", "other_name": "missing-other"},
        )

    assert result.is_error is True
    assert (
        _text(result)
        == "Error executing tool diff_pages: page not found: missing-other.md"
    )
    # Both GETs were issued — the first read succeeded (so the
    # handler proceeded to the second), then the second read
    # 404'd.
    assert calls == [
        ("GET", "/.fs/present.md"),
        ("GET", "/.fs/missing-other.md"),
    ]


@pytest.mark.asyncio
async def test_diff_pages_5xx_on_first_read_returns_tool_error() -> None:
    """A 5xx on the first read surfaces the same wording as the read tool.

    :func:`_translate_sb_errors` threads the same exception
    translation through every tool that wraps an ``sb_client``
    call; ``diff_pages`` is no exception. The error message is
    the SB-side ``silverbullet error: <status>`` wording, and
    no second read is attempted.
    """
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        return httpx.Response(500, text="internal error")

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "diff_pages",
            {"name": "any.md", "other_name": "second.md"},
        )

    assert result.is_error is True
    assert (
        _text(result)
        == "Error executing tool diff_pages: silverbullet error: 500"
    )
    assert calls == [("GET", "/.fs/any.md")]


@pytest.mark.asyncio
async def test_diff_pages_timeout_on_first_read_returns_tool_error() -> None:
    """An httpx timeout on the first read → ``\"silverbullet request timed out\"``.

    Same translation as the read tool: ``httpx.TimeoutException``
    on a wrapped ``sb_client`` call surfaces as a ToolError with
    the design-doc wording, so the agent sees one error shape
    across every tool.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("simulated")

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "diff_pages",
            {"name": "any.md", "other_body": "literal"},
        )

    assert result.is_error is True
    assert (
        _text(result)
        == "Error executing tool diff_pages: silverbullet request timed out"
    )


@pytest.mark.asyncio
async def test_diff_pages_does_not_issue_writes() -> None:
    """``diff_pages`` is read-only — no PUT / DELETE / PATCH requests.

    Locks the no-side-effects contract the ticket calls out:
    ``diff_pages`` is a read tool. The handler tracks every
    request method and asserts only GETs were issued. A future
    refactor that accidentally threads a write into the diff
    flow (e.g. caching the diff server-side) would surface as
    test failure rather than a silent SB mutation.
    """
    methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        return httpx.Response(200, text="body", headers={"ETag": '"e"'})

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "diff_pages",
            {"name": "a.md", "other_name": "b"},
        )

    assert result.is_error is False
    assert methods == ["GET", "GET"]  # one read per page, no writes


# --- list_tasks (T29) --------------------------------------------------
#
# ``list_tasks`` returns checkbox bullets parsed from a page body
# (per-page form, always available via ``sb_client.read_page``) or
# across the whole space (space-walk form, gated by the journal
# config). T29 ticket wire shape: ``{name, ref, line, state, text}``.
# State is one of ``" "`` / ``"x"`` / ``"X"`` (SB's three checkbox
# markers); ``ref`` is the wikilink target on the same line, or
# ``None`` for non-addressable bullets.


@pytest.mark.asyncio
async def test_list_tasks_returns_empty_when_page_has_no_bullets() -> None:
    """A page with no checkbox bullets → empty list.

    Empty result (not an error) — the agent reading a page with
    no tasks shouldn't get a confusing "tool failed" back.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="# just a heading\n\nno tasks here\n")

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "list_tasks", {"page": "Areas/Kanban"}
        )
    # T29 wire shape is a bare ``list[...]``, but the SDK wraps
    # ``list[X]`` returns in ``{"result": [...]}`` for
    # ``structured_content`` (the same shape ``recent_pages`` and
    # ``pages_touching_topic`` use).
    assert result.is_error is False
    assert result.structured_content == {"result": []}


@pytest.mark.asyncio
async def test_list_tasks_carries_name_ref_line_state_text() -> None:
    """Each entry has the five T29 fields.

    The wire shape is fixed by T29 — adding a field is a
    breaking change (an agent that destructures the dict will
    miss the new key). Removing a field is also a breaking
    change. This test pins the field set in place so a future
    refactor that drops one surfaces loudly.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text=(
                "# kanban\n"
                "- [ ] todo with [[Pages/Hobbies]] ref\n"
                "- [x] done item\n"
                "- [X] cancelled item\n"
                "- [ ] no-ref bullet\n"
            ),
        )

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "list_tasks", {"page": "Areas/Kanban"}
        )

    sc = result.structured_content
    assert sc["result"] == [
        {"name": "Areas/Kanban.md", "ref": "Pages/Hobbies", "line": 2, "state": " ", "text": "todo with [[Pages/Hobbies]] ref"},
        {"name": "Areas/Kanban.md", "ref": None, "line": 3, "state": "x", "text": "done item"},
        {"name": "Areas/Kanban.md", "ref": None, "line": 4, "state": "X", "text": "cancelled item"},
        {"name": "Areas/Kanban.md", "ref": None, "line": 5, "state": " ", "text": "no-ref bullet"},
    ]


@pytest.mark.asyncio
async def test_list_tasks_strips_wikilink_alias_to_target() -> None:
    """``[[target|alias]]`` → ref = ``"target"`` (no alias).

    SB's editor resolves ``externalTaskRef`` to the wikilink
    *target*, not the display text. The bridge matches the
    editor: the alias is stripped so an agent calling
    ``check_task(page, "Pages/Hobbies")`` toggles the right
    bullet.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text="- [ ] read the card [[Pages/Hobbies#card|read the card]]\n",
        )

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("list_tasks", {"page": "x"})

    assert result.structured_content["result"][0]["ref"] == "Pages/Hobbies#card"


@pytest.mark.asyncio
async def test_list_tasks_picks_first_wikilink_on_multi_ref_line() -> None:
    """A bullet with multiple ``[[wikilink]]`` tokens keeps the first ref.

    Rare in the wild but seen on lines that mention two related
    pages. The editor's ``externalTaskRef`` resolves to the
    *first* wikilink — the bridge matches.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text="- [ ] see [[First]] and [[Second]]\n",
        )

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("list_tasks", {"page": "x"})

    [entry] = result.structured_content["result"]
    assert entry["ref"] == "First"


@pytest.mark.asyncio
async def test_list_tasks_skips_frontmatter_bullets() -> None:
    """``- [ ]`` inside a frontmatter block is YAML config, not a task.

    A page with ``tags: ...`` frontmatter often has
    ``- [ ]`` lines that are config keys (YAML block-list
    items). The bridge skips the frontmatter block so a
    config-key ``- [ ]`` doesn't surface as a task — that
    would confuse the agent into thinking the page has a
    todo item it doesn't actually have.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text=(
                "---\n"
                "tags:\n"
                "  - foo\n"
                "  - bar\n"
                "---\n"
                "# heading\n"
                "- [ ] real task after frontmatter\n"
            ),
        )

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("list_tasks", {"page": "x"})

    sc = result.structured_content
    # Only the real task survives; the YAML block-list items are
    # not tasks.
    assert len(sc["result"]) == 1
    assert sc["result"][0]["ref"] is None
    assert sc["result"][0]["text"] == "real task after frontmatter"
    # Line numbers are editor-shaped (frontmatter included), so
    # this task is on editor line 6 (1=---, 2=tags:, 3=foo, 4=bar,
    # 5=---, 6=# heading, 7=real task). Wait — the heading is
    # line 6 and the task is line 7. Let me re-check by counting:
    # the body in this test is split as
    # ``["---", "tags:", "  - foo", "  - bar", "---", "# heading",
    # "- [ ] real task after frontmatter"]``. 1-indexed, the task
    # is on line 7.
    assert sc["result"][0]["line"] == 7


@pytest.mark.asyncio
async def test_list_tasks_line_numbers_are_editor_shaped() -> None:
    """``line`` field is 1-indexed against the full body (frontmatter included).

    An agent that wants to ``patch_page_lines(name, line=N)``
    needs ``N`` to point at the same line the editor
    highlights. Without frontmatter, line numbers happen to
    equal body-line numbers; with frontmatter, they don't —
    this test pins the editor-shaped convention.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text=(
                "---\n"      # line 1 (opening fence)
                "tags: foo\n"  # line 2
                "---\n"      # line 3 (closing fence)
                "\n"         # line 4 (blank)
                "- [ ] task\n"  # line 5
            ),
        )

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("list_tasks", {"page": "x"})

    [entry] = result.structured_content["result"]
    assert entry["line"] == 5


@pytest.mark.asyncio
async def test_list_tasks_nested_bullets_are_addressable() -> None:
    """Indented ``- [ ]`` lines under an outer bullet count as tasks.

    SB's editor treats nested checkbox bullets as addressable
    tasks (a kanban sub-task), so the bridge does too. Quick
    check that the parser's leading-whitespace prefix in
    ``_TASK_BULLET_RE`` doesn't accidentally reject indented
    bullets.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text=(
                "- [ ] outer\n"
                "  - [ ] nested-1 [[Inner1]]\n"
                "    - [ ] deep [[Inner2]]\n"
            ),
        )

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("list_tasks", {"page": "x"})

    sc = result.structured_content
    assert len(sc["result"]) == 3
    assert sc["result"][0]["text"] == "outer"
    assert sc["result"][1]["ref"] == "Inner1"
    assert sc["result"][2]["ref"] == "Inner2"


@pytest.mark.asyncio
async def test_list_tasks_404_returns_tool_error() -> None:
    """Page not found surfaces the design-doc 404 wording.

    Same wording as ``read_page`` — the read tool translates
    the SB 404 to ``ToolError("page not found: <name>")`` and
    ``list_tasks`` reuses :func:`_translate_sb_errors` to keep
    the wording in one place.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="page not found")

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "list_tasks", {"page": "missing"}
        )

    assert result.is_error is True
    assert _text(result) == "Error executing tool list_tasks: page not found: missing.md"


@pytest.mark.asyncio
async def test_list_tasks_5xx_returns_tool_error() -> None:
    """SB 5xx surfaces the unified ``ToolError("silverbullet error: <status>")`` wording.

    The same translation :func:`_translate_sb_errors` applies
    to every ``/.fs``-backed tool. ``list_tasks`` (T29) routes
    the read through that helper so an SB-side failure looks
    identical to the agent regardless of which tool surfaced
    it.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="upstream gone")

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("list_tasks", {"page": "x"})

    assert result.is_error is True
    assert _text(result) == "Error executing tool list_tasks: silverbullet error: 503"


@pytest.mark.asyncio
async def test_list_tasks_timeout_returns_tool_error() -> None:
    """httpx timeout → ``ToolError("silverbullet request timed out")``.

    Same wording as every other ``/.fs``-backed tool. The
    timeout exception type (``httpx.TimeoutException``) is
    caught by :func:`_translate_sb_errors` so ``list_tasks``
    doesn't need its own timeout clause.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("simulated")

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("list_tasks", {"page": "x"})

    assert result.is_error is True
    assert _text(result) == "Error executing tool list_tasks: silverbullet request timed out"


@pytest.mark.asyncio
async def test_list_tasks_without_page_without_journal_root_errors() -> None:
    """``page=None`` with the journal gate off → ``ToolError``.

    The space-walk variant requires direct FS access (because
    SB doesn't expose a "list every task on every page"
    endpoint over HTTP — we'd otherwise need N+1 round trips
    per ``list_pages`` + ``read_page``). On a sidecar without
    a volume mount the gate is off and the space-walk branch
    surfaces a clear error so the agent knows to fall back
    to the per-page form (``list_tasks(page="...")``).
    """
    server = _build(lambda req: httpx.Response(200))
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("list_tasks", {})

    assert result.is_error is True
    text = _text(result)
    assert "list_tasks without page argument" in text
    assert "MCP_SILVERBULLET_JOURNAL_TOOLS" in text


# --- check_task (T30) --------------------------------------------------
#
# ``check_task(page, ref, state=\"done\", if_match?, dry_run=False)``
# flips a checkbox bullet's state by its wikilink ref. The
# implementation is a read-modify-write: ``GET /.fs/{page}`` →
# :func:`_find_task_bullet` (locate the unique bullet whose
# wikilink equals ``ref``) → flip the marker (``[ ]`` / ``[x]`` /
# ``[X]``) → ``PUT /.fs/{page}`` (with ``If-Match: <read_etag>``)
# so a concurrent edit fails 412 rather than silently clobbering
# the flip. ``dry_run=True`` skips the PUT and returns the T26
# preview envelope (``{dry_run, original, patched, diff}``).
#
# Four application-level error surfaces that fire without a SB
# round trip or between the read and the write:
# 1. Empty ``ref`` upfront → ``ref must not be empty``.
# 2. ``state`` not in ``{\"done\", \"todo\", \"cancelled\"}`` →
#    ``state must be one of: done, todo, cancelled``.
# 3. No bullet on the page has a wikilink matching ``ref`` →
#    ``no task with ref {ref} on page {page}; the task may
#    not have a wikilink ref or may live on a different page``.
# 4. Multiple bullets have matching wikilinks → ``ref {ref}
#    matches multiple tasks on page {page}; narrow the ref
#    or use patch_page_lines directly`` (count is in the wording
#    is implicit via \"multiple\"; the explicit count lives
#    on the parser count, not the wire wording).
#
# Wire-level surfaces (404 / 412 / 5xx / timeout / 413) go
# through :func:`server._translate_sb_errors` for the read and
# the write separately, matching the read / write tools' wording.


@pytest.mark.asyncio
async def test_check_task_flips_todo_to_done_with_default_state() -> None:
    """``check_task`` with default ``state=\"done\"`` flips ``[ ]`` → ``[x]``.

    The whole ``read_page`` → flip → ``write_page`` cycle runs
    in one tool call. The page body is read, the unique
    bullet with the matching wikilink is located, the marker
    is flipped, and the body is written back with
    ``If-Match: <read_etag>`` so a concurrent edit fails 412.
    """
    body = "header\n- [ ] task [[Ref]] ref\n"
    written_bodies: list[str] = []
    written_if_match: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200,
                text=body,
                headers={"ETag": '"read-etag"'},
            )
        if request.method == "PUT":
            written_bodies.append(request.read().decode("utf-8"))
            written_if_match.append(request.headers.get("If-Match"))
            return httpx.Response(
                200,
                text="",
                headers={
                    "ETag": '"write-etag"',
                    "X-Content-Length": str(
                        len(written_bodies[-1].encode("utf-8"))
                    ),
                    "X-Last-Modified": "1700000000000",
                },
            )
        return httpx.Response(500)

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "check_task", {"page": "p", "ref": "Ref"}
        )

    assert result.is_error is False
    assert written_bodies == ["header\n- [x] task [[Ref]] ref\n"]
    # ``If-Match`` on the write is the read's etag, threaded
    # automatically so the caller doesn't have to manage an
    # etag round-trip just to flip a task.
    assert written_if_match == ['"read-etag"']
    # The T23 acknowledgement envelope — the write's meta, not
    # the read's (same carry-forward as ``append_to_page``).
    sc = result.structured_content
    assert sc is not None
    assert sc["name"] == "p.md"
    assert sc["etag"] == '"write-etag"'
    assert sc["last_modified_ms"] == 1700000000000


@pytest.mark.asyncio
async def test_check_task_returns_todo_state() -> None:
    """``state=\"todo\"`` flips ``[x]`` → ``[ ]`` (the uncheck direction).

    Round-tripping done → todo works the same way todo → done
    does — the marker character in :data:`_STATE_TO_MARKER` is
    the only thing that changes.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200,
                text="- [x] task [[Ref]] ref\n",
                headers={"ETag": '"e"'},
            )
        if request.method == "PUT":
            return httpx.Response(
                200,
                text="",
                headers={
                    "ETag": '"w"',
                    "X-Content-Length": str(
                        len(request.read())
                    ),
                },
            )
        return httpx.Response(500)

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "check_task", {"page": "p", "ref": "Ref", "state": "todo"}
        )
    assert result.is_error is False


@pytest.mark.asyncio
async def test_check_task_returns_cancelled_state() -> None:
    """``state=\"cancelled\"`` flips ``[ ]`` → ``[X]`` (SB's third state).

    Uppercase ``X`` distinguishes cancelled from done
    (lowercase ``x``); the parser keeps the case so the
    agent can read the state directly off the marker.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200,
                text="- [ ] task [[Ref]] ref\n",
                headers={"ETag": '"e"'},
            )
        if request.method == "PUT":
            body = request.read().decode("utf-8")
            assert body == "- [X] task [[Ref]] ref\n"
            return httpx.Response(
                200, text="", headers={"ETag": '"w"'}
            )
        return httpx.Response(500)

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "check_task",
            {"page": "p", "ref": "Ref", "state": "cancelled"},
        )
    assert result.is_error is False


@pytest.mark.asyncio
async def test_check_task_rejects_unknown_state_upfront() -> None:
    """``state=\"complete\"`` (or any other typo) → ``ToolError`` upfront.

    No GET, no PUT — the validation runs before the read.
    The wording carries the allowed set so the agent sees
    what it should have passed without trial-and-error.
    ``assert_called_with(GET, ...)`` is enforced by the
    transport's empty request log below.
    """
    requests_made: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests_made.append(request.method)
        return httpx.Response(200, text="")

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "check_task",
            {"page": "p", "ref": "Ref", "state": "complete"},
        )

    assert result.is_error is True
    text = _text(result)
    assert "state must be one of" in text
    assert "done" in text
    assert "todo" in text
    assert "cancelled" in text
    # Upfront validation — no GET happened.
    assert requests_made == []


@pytest.mark.asyncio
async def test_check_task_rejects_empty_ref_upfront() -> None:
    """``ref=\"\"`` → ``ToolError(\"ref must not be empty\")`` upfront.

    Mirrors the other read-modify-write tools' upfront guards
    (``append_to_page`'s empty-text, ``patch_page_replace`'s
    empty-find). An empty ref is almost certainly a caller bug;
    surfacing it loudly pins the bug at the call site and saves
    the read round trip.
    """
    requests_made: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests_made.append(request.method)
        return httpx.Response(200, text="")

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "check_task", {"page": "p", "ref": ""}
        )

    assert result.is_error is True
    assert "ref must not be empty" in _text(result)
    assert requests_made == []


@pytest.mark.asyncio
async def test_check_task_errors_when_no_bullet_matches_ref() -> None:
    """No bullet with the ref → ``no task with ref {ref} on page {page}; …``.

    The wording points the agent at the two most likely
    causes (no wikilink ref on the bullet; the task lives on
    a different page). Distinct from the 404 ``page not
    found`` wording so the agent can tell a *page exists
    but no matching task* from a *page is missing*.
    """
    requests_made: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests_made.append(request.method)
        if request.method == "GET":
            return httpx.Response(
                200,
                text="- [ ] task [[Other]] ref\n",
                headers={"ETag": '"e"'},
            )
        return httpx.Response(500)

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "check_task", {"page": "p", "ref": "Pages/Hobbies"}
        )

    assert result.is_error is True
    text = _text(result)
    assert "no task with ref Pages/Hobbies" in text
    assert "on page p" in text
    # The read happened (we needed to know which tasks exist)
    # but no PUT (no flip should land when there's no match).
    assert requests_made == ["GET"]


@pytest.mark.asyncio
async def test_check_task_errors_when_multiple_bullets_match_ref() -> None:
    """Multiple bullets with the same ref → ``matches multiple tasks`` wording.

    Disambiguation hint in the error: the caller should
    narrow the ref (use a more specific wikilink target) or
    fall back to :func:`patch_page_lines` directly. Without
    this error, ``check_task`` would silently flip the *first*
    one — a confusing UX for a typo'd ref that's already in
    use.
    """
    requests_made: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests_made.append(request.method)
        if request.method == "GET":
            return httpx.Response(
                200,
                text=(
                    "- [ ] first [[Same]]\n"
                    "- [ ] second [[Same]]\n"
                ),
                headers={"ETag": '"e"'},
            )
        return httpx.Response(500)

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "check_task", {"page": "p", "ref": "Same"}
        )

    assert result.is_error is True
    text = _text(result)
    assert "matches multiple tasks" in text
    assert "on page p" in text
    assert "patch_page_lines" in text
    assert requests_made == ["GET"]


@pytest.mark.asyncio
async def test_check_task_404_returns_tool_error() -> None:
    """Page missing → standard ``ToolError(\"page not found: {name}\")`` wording.

    Same translation :func:`_translate_sb_errors` provides for
    every ``/.fs``-backed tool. ``check_task`` wraps the read
    in this helper so an SB-side missing page looks identical
    to the agent regardless of which tool surfaced it.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="page not found")

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "check_task", {"page": "missing", "ref": "Ref"}
        )

    assert result.is_error is True
    assert _text(result) == "Error executing tool check_task: page not found: missing.md"


@pytest.mark.asyncio
async def test_check_task_stale_if_match_returns_412_tool_error() -> None:
    """Caller passed ``if_match=<stale_etag>`` → unified 412 wording.

    The read succeeds (the page exists, the etag was just
    ``read_etag``); the write fails 412 because the caller's
    ``if_match`` doesn't match the body the bridge would
    write. The wording is the unified 412 / precondition-failed
    shape the rest of the bridge uses so the agent sees one
    error across all tools.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200,
                text="- [ ] task [[Ref]] ref\n",
                headers={"ETag": '"read-etag"'},
            )
        if request.method == "PUT":
            return httpx.Response(412, text="precondition failed")
        return httpx.Response(500)

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "check_task",
            {"page": "p", "ref": "Ref", "if_match": '"stale-etag"'},
        )

    assert result.is_error is True
    assert _text(result) == "Error executing tool check_task: precondition failed; check if_match/if_none_match"


@pytest.mark.asyncio
async def test_check_task_dry_run_returns_envelope_without_writing() -> None:
    """``dry_run=True`` → T26 envelope, no PUT issued.

    Read still happens (the tool needs the body to compute
    the patched version), the in-memory flip is computed,
    ``If-Match`` is checked against the read's etag, and the
    tool returns ``{dry_run: True, original: str, patched:
    str, diff: str}``. The Layer-1 test asserts on the
    request methods the bridge issues (no PUT on dry-run)
    so a future refactor that mistakenly issues a write on
    the dry-run path surfaces as test failure.
    """
    requests_made: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests_made.append(request.method)
        if request.method == "GET":
            return httpx.Response(
                200,
                text="- [ ] task [[Ref]] ref\n",
                headers={"ETag": '"e"'},
            )
        return httpx.Response(500)

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "check_task",
            {"page": "p", "ref": "Ref", "dry_run": True},
        )

    assert result.is_error is False
    sc = result.structured_content
    assert sc is not None
    assert sc["dry_run"] is True
    assert sc["original"] == "- [ ] task [[Ref]] ref\n"
    assert sc["patched"] == "- [x] task [[Ref]] ref\n"
    assert "- [ ]" in sc["diff"]
    assert "- [x]" in sc["diff"]
    # The dry-run path: read happened, write did not.
    assert requests_made == ["GET"]


@pytest.mark.asyncio
async def test_check_task_dry_run_stale_if_match_raises_412_tool_error() -> None:
    """``dry_run=True`` + ``if_match=<stale>`` → 412-equivalent ToolError.

    The dry-run path doesn't issue a PUT, so SB never gets to
    enforce the precondition; the bridge-side mirror
    (:func:`_validate_if_match_on_read`) catches the stale
    etag so the agent sees the same wording as the live path.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200,
                text="- [ ] task [[Ref]] ref\n",
                headers={"ETag": '"read-etag"'},
            )
        return httpx.Response(500)

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "check_task",
            {
                "page": "p",
                "ref": "Ref",
                "if_match": '"stale-etag"',
                "dry_run": True,
            },
        )

    assert result.is_error is True
    text = _text(result)
    assert "precondition failed" in text


@pytest.mark.asyncio
async def test_check_task_dry_run_empty_ref_still_errors() -> None:
    """``dry_run=True`` + ``ref=\"\"`` → upfront ``ref must not be empty``.

    Pre-read input validation fires on the dry-run path the
    same way it does on the live path — a caller that passes
    a bad input gets the same specific ToolError, not a
    vague preview.
    """
    requests_made: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests_made.append(request.method)
        return httpx.Response(200, text="")

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "check_task",
            {"page": "p", "ref": "", "dry_run": True},
        )

    assert result.is_error is True
    assert "ref must not be empty" in _text(result)
    assert requests_made == []


@pytest.mark.asyncio
async def test_check_task_dry_run_no_match_still_errors() -> None:
    """``dry_run=True`` + missing ref → ``no task with ref …`` (no PUT).

    The application-level \"no match\" error fires between
    the read and the write on the live path; on the dry-run
    path it fires before the dry-run envelope is built, so
    a caller with a typo'd ref never sees a fake preview.
    """
    requests_made: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests_made.append(request.method)
        if request.method == "GET":
            return httpx.Response(
                200,
                text="- [ ] task [[Other]] ref\n",
                headers={"ETag": '"e"'},
            )
        return httpx.Response(500)

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "check_task",
            {
                "page": "p",
                "ref": "Pages/Hobbies",
                "dry_run": True,
            },
        )

    assert result.is_error is True
    assert "no task with ref Pages/Hobbies" in _text(result)
    assert requests_made == ["GET"]


@pytest.mark.asyncio
async def test_check_task_5xx_returns_tool_error() -> None:
    """SB 5xx surfaces the unified ``ToolError(\"silverbullet error: <status>\")`` wording.

    Same translation :func:`_translate_sb_errors` provides for
    every ``/.fs``-backed tool. The read 5xx surfaces here;
    a write 5xx would surface the same way under the live path.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="upstream gone")

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "check_task", {"page": "x", "ref": "Ref"}
        )

    assert result.is_error is True
    assert _text(result) == "Error executing tool check_task: silverbullet error: 503"


@pytest.mark.asyncio
async def test_check_task_timeout_returns_tool_error() -> None:
    """httpx timeout → ``ToolError(\"silverbullet request timed out\")``.

    Same wording as every other ``/.fs``-backed tool. The
    timeout exception type (``httpx.TimeoutException``) is
    caught by :func:`_translate_sb_errors` so ``check_task``
    doesn't need its own timeout clause.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("simulated")

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "check_task", {"page": "x", "ref": "Ref"}
        )

    assert result.is_error is True
    assert _text(result) == "Error executing tool check_task: silverbullet request timed out"

# --- T31b: post-write concurrency-token verification ---------------------


@pytest.mark.asyncio
async def test_t31b_write_page_detects_concurrent_edit_via_silent_overwrite() -> None:
    """200 write followed by a re-read with a drifted etag surfaces ``concurrent edit detected``.

    T31 closed negatively on this dev box (SB ignores ``If-Match``
    on PUT, returns no ``ETag``). Without T31b the v1.2 / v1.3
    concurrency story was unsupported: an agent that does
    ``read → write(if_match=read_etag)`` silently overwrites a
    page a concurrent agent already updated. T31b's
    post-write verification helper re-reads the page and
    compares etags — a drifted etag raises
    ``ToolError("concurrent edit detected: …")`` *before* the
    T23 ack envelope is constructed, so the agent sees the
    same conflict signal it would have seen on an SB that
    honored ``If-Match``, just delivered later in the round
    trip.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT":
            # SB returns 200 on stale ``If-Match`` (the v1.3 SB
            # fact T31 surfaced); the bridge's post-write
            # verification is what catches the drift.
            return httpx.Response(200, headers={"ETag": '"new"'})
        # Verification GET returns the post-write etag, which
        # drifts from the caller's ``"v1"`` — the page was
        # mutated out-of-band between the agent's read and
        # write.
        return httpx.Response(200, headers={"ETag": '"new"'})

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "write_page",
            {"name": "index.md", "content": "body", "if_match": '"v1"'},
        )

    assert result.is_error is True
    assert "concurrent edit detected" in _text(result)
    # The error names the expected etag so the agent can see
    # what the bridge was looking for.
    assert '"v1"' in _text(result)


@pytest.mark.asyncio
async def test_t31b_write_page_verification_passes_when_etag_unchanged() -> None:
    """Happy path: 200 write followed by a re-read with the same etag → success.

    The mirror of the negative case: the helper compares the
    post-write re-read against the caller's ``if_match``; if
    they match (no concurrent edit), the helper no-ops and the
    tool returns the normal T23 ack envelope. This locks the
    ``happy path is unaffected`` invariant — a T31b
    regression that fires on every write would be loudest as
    a 100% failure of this test, not as a flaky edge case.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT":
            return httpx.Response(200, headers={"ETag": '"v1"'})
        # Verification GET returns the same etag.
        return httpx.Response(200, headers={"ETag": '"v1"'})

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "write_page",
            {"name": "index.md", "content": "body", "if_match": '"v1"'},
        )

    assert result.is_error is False
    payload = result.structured_content or {}
    assert payload.get("name") == "index.md"
    assert payload.get("etag") == '"v1"'


@pytest.mark.asyncio
async def test_t31b_write_page_verification_passes_when_synthesized_etag_unchanged() -> None:
    """Happy path on the synthesized-etag fallback path: same body on read and re-read → success.

    The realistic-shape mock the prior test's contrived ETag
    mock hides: SB returns no ``ETag`` header on this dev box
    (the v1.3 T31 fact), so both the PUT response and the
    verification GET fall through to the synthesized
    ``"{ms}-{bytes}"`` path. The bridge stamps
    ``X-Last-Modified`` with ``now_ms`` on every PUT request
    (``sb_client.py:411``), so the pre-write read's mtime
    and the post-write re-read's mtime differ — but the body
    is unchanged. A synthesized etag that includes the mtime
    drifts across the read-modify-write dance and the T31b
    helper raises "concurrent edit detected" on every
    successful write. T44 drops the mtime component so the
    synthesized etag is stable across re-reads of the same
    body.

    This is the user's defect (2026-08-31): the bridge
    raises ``ToolError("concurrent edit detected: …")`` on
    every successful write where the underlying write
    actually succeeded. The Grok Automations script handled
    it transparently; the bridge should not lie about 412 in
    the first place.
    """
    # Two distinct ``X-Last-Modified`` values, both in the
    # 1.7e12 epoch-ms range. The body size (4 bytes for
    # ``"body"``) stays the same across both reads.
    pre_mtime = "1700000000000"
    post_mtime = "1700000001000"

    def handler(request: httpx.Request) -> httpx.Response:
        # First read: the caller's pre-write read. SB echoes
        # the ``pre_mtime`` it stored.
        if request.method == "GET" and handler.first_read_done is False:
            handler.first_read_done = True
            return httpx.Response(
                200,
                text="body",
                headers={
                    "X-Last-Modified": pre_mtime,
                    "X-Content-Length": "4",
                },
            )
        # PUT: SB echoes the bridge's request header
        # (``X-Last-Modified`` = ``post_mtime``, the
        # bridge's ``now_ms`` stamp).
        if request.method == "PUT":
            return httpx.Response(
                200,
                headers={
                    "X-Last-Modified": post_mtime,
                    "X-Content-Length": "4",
                },
            )
        # Verification GET: SB echoes ``post_mtime`` again.
        return httpx.Response(
            200,
            text="body",
            headers={
                "X-Last-Modified": post_mtime,
                "X-Content-Length": "4",
            },
        )

    handler.first_read_done = False  # type: ignore[attr-defined]

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        # The agent's read-modify-write dance: read the page,
        # then write the same body with the read's etag as
        # ``if_match``. T31b's helper will re-read after the
        # write; with the size-only synthesized etag, the
        # post-write re-read's etag matches the pre-read's
        # etag (both ``'"4"'``) and the helper no-ops.
        read = await client.call_tool("read_page", {"name": "index.md"})
        assert read.is_error is False
        read_etag = (read.structured_content or {}).get("etag")
        assert read_etag is not None
        result = await client.call_tool(
            "write_page",
            {"name": "index.md", "content": "body", "if_match": read_etag},
        )

    assert result.is_error is False, (
        f"T31b (T44): synthesized-etag post-write re-read "
        f"drifted on an unchanged body; the bridge raised "
        f"{_text(result)!r} instead of returning the T23 "
        f"ack envelope. The user's defect: the bridge "
        f"raises 'concurrent edit detected' on every "
        f"successful write when SB strips the ETag header."
    )
    payload = result.structured_content or {}
    assert payload.get("name") == "index.md"
    # The synthesized etag is now ``"{size_bytes}"`` — no
    # mtime component, so it's stable across reads of the
    # same body.
    assert payload.get("etag") == '"4"'


@pytest.mark.asyncio
async def test_t31b_write_page_skips_verification_when_if_match_is_none() -> None:
    """``if_match=None`` opts out of the verification (no precondition to verify).

    The helper's contract: ``None`` and ``"*"`` are both no-ops
    (no value to compare against). When the caller passes
    ``if_match=None`` they explicitly opted out of the
    concurrency primitive — there's no race the helper can
    detect. Locks the "opt-in only" invariant: a regression
    that fires verification on every write would surface here
    as a 200 response turning into an error.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        # No follow-up GET expected: the helper skips when
        # ``if_match`` is ``None``. Handler returns 200 for any
        # method just in case a future regression adds one.
        if request.method == "PUT":
            return httpx.Response(200, headers={"ETag": '"v2"'})
        return httpx.Response(200, headers={"ETag": '"v2"'})

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "write_page",
            {"name": "index.md", "content": "body"},
        )

    assert result.is_error is False


@pytest.mark.asyncio
async def test_t31b_write_page_skips_verification_when_if_match_is_star() -> None:
    """``if_match="*"`` (require existence) opts out of verification too.

    ``"*"`` doesn't uniquely identify a body; comparing it
    against a real etag would always mismatch. The helper's
    ``if expected_etag == "*": return`` clause makes the
    ``write_page(if_match="*")`` path (which is what
    ``create_page`` / T32 delegates to) safe — the helper
    won't fire on the create path and cause every create to
    surface as a concurrency error.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT":
            return httpx.Response(200, headers={"ETag": '"whatever"'})
        return httpx.Response(200, headers={"ETag": '"whatever"'})

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "write_page",
            {"name": "index.md", "content": "body", "if_match": "*"},
        )

    assert result.is_error is False


@pytest.mark.asyncio
async def test_t31b_append_to_page_detects_concurrent_edit_via_silent_overwrite() -> None:
    """Read-modify-write tools inherit the same verification on their final PUT.

    T31b's contract applies to every write tool that threads
    an etag through ``if_match`` — read-modify-write tools do
    that explicitly (they read the page, splice, and re-write
    with the read's etag as the precondition). The negative
    case is identical to ``write_page``'s: 200 on the PUT
    followed by a re-read with a drifted etag surfaces the
    unified ``concurrent edit detected`` wording.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            # First GET is the read-modify-write's read.
            return httpx.Response(200, text="hello")
        if request.method == "PUT":
            return httpx.Response(200, headers={"ETag": '"new"'})
        # Verification GET returns the drifted etag.
        return httpx.Response(200, headers={"ETag": '"new"'})

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "append_to_page",
            {"name": "index.md", "text": "world", "if_match": '"v1"'},
        )

    assert result.is_error is True
    assert "concurrent edit detected" in _text(result)


@pytest.mark.asyncio
async def test_t31b_append_to_page_auto_threads_read_etag_when_if_match_is_none() -> None:
    """``if_match=None`` on append_to_page auto-threads the read's etag for the verification path.

    Read-modify-write tools thread the read's etag into the
    write's ``if_match`` automatically (the v1.2 read-modify-
    write contract). The T31b helper sees that auto-threaded
    value as ``expected_etag`` and re-reads after the write —
    so a concurrent edit between read and write surfaces as
    the unified concurrency error, even when the caller
    passed ``if_match=None``. This is the operational canary
    for the v1.3 story: an agent that just wants ``append``
    (no etag round-trip) still gets concurrent-edit detection
    for free.
    """
    request_count = {"get": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            request_count["get"] += 1
            # First GET (the read-modify-write) returns
            # ``"read-etag"``, which the bridge auto-threads
            # into the write's ``If-Match``. Second GET (the
            # verification re-read) returns ``"new"`` — drift
            # means a concurrent edit happened between the
            # read and the write, and the helper surfaces the
            # unified concurrency error.
            if request_count["get"] == 1:
                return httpx.Response(
                    200, text="hello", headers={"ETag": '"read-etag"'}
                )
            return httpx.Response(200, headers={"ETag": '"new"'})
        if request.method == "PUT":
            return httpx.Response(200, headers={"ETag": '"new"'})
        return httpx.Response(200, headers={"ETag": '"new"'})

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "append_to_page",
            {"name": "index.md", "text": "world"},
        )

    assert result.is_error is True
    assert "concurrent edit detected" in _text(result)


@pytest.mark.asyncio
async def test_t31b_dry_run_skips_verification() -> None:
    """``dry_run=True`` paths do not invoke the verification helper.

    The helper's contract: ``dry_run=True`` short-circuits to
    a no-op because no write happened to verify. A regression
    that fired verification on the dry-run path would turn
    every dry-run into a concurrency check against the
    pre-write read, which is meaningless — there's no PUT
    to race against. Locks the "dry-run is read-only" invariant.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        # Only GETs expected (read + dry-run's pre-read; no PUT).
        return httpx.Response(200, text="hello", headers={"ETag": '"v1"'})

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "append_to_page",
            {"name": "index.md", "text": "world", "if_match": '"v1"', "dry_run": True},
        )

    assert result.is_error is False
    payload = result.structured_content or {}
    assert payload.get("dry_run") is True


@pytest.mark.asyncio
async def test_t31b_verification_skips_on_re_read_404() -> None:
    """Re-read returning 404 (page deleted out-of-band post-write) is a no-op, not an error.

    The helper's contract: a 404 on the verification re-read is
    treated as "verification skipped" (not a concurrency
    violation). A regression that fired the helper on a 404
    would turn every post-delete re-read into a spurious
    concurrency error. Locks the "post-delete re-read is safe"
    invariant — important for ``delete_page`` (which has no
    PUT to verify) and ``move_page`` (where the source
    re-read post-delete 404s by construction).
    """
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT":
            return httpx.Response(200, headers={"ETag": '"v1"'})
        # Verification GET returns 404 (page deleted
        # out-of-band).
        return httpx.Response(404, text="not found")

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "write_page",
            {"name": "index.md", "content": "body", "if_match": '"v1"'},
        )

    assert result.is_error is False


@pytest.mark.asyncio
async def test_t31b_verification_skips_on_re_read_5xx() -> None:
    """Re-read returning 5xx degrades gracefully (no false-positive concurrency error).

    The helper's contract: transient SB failures during the
    re-read are treated as "verification skipped" rather than
    as a concurrency violation. The alternative is a
    false-positive "concurrent edit detected" on a flaky SB
    that would be much harder to debug than a silently-lost
    verification. Locks the "transient failures are
    best-effort" invariant.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT":
            return httpx.Response(200, headers={"ETag": '"v1"'})
        return httpx.Response(503, text="upstream gone")

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "write_page",
            {"name": "index.md", "content": "body", "if_match": '"v1"'},
        )

    assert result.is_error is False


@pytest.mark.asyncio
async def test_t31b_412_wins_over_verification_on_sbs_that_honor_if_match() -> None:
    """When SB returns 412, the unified 412 wording wins (no follow-up re-read).

    T31b's contract: the helper is the *fallback* for SBs that
    don't honor ``If-Match`` — the primary path is the 412
    from :func:`_translate_sb_errors`. When SB returns 412,
    the helper doesn't run (the request already raised a
    typed exception, which the helper's caller propagates
    through ``_translate_sb_errors``). This test pins that
    the 412 path *still* wins even with T31b in place — a
    regression that bypassed the 412 path on this branch
    would surface here.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(412, text="precondition failed")

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "write_page",
            {"name": "index.md", "content": "body", "if_match": '"v1"'},
        )

    assert result.is_error is True
    assert "precondition failed" in _text(result)


@pytest.mark.asyncio
async def test_t31b_delete_page_post_delete_verification_skips() -> None:
    """``delete_page``'s post-delete verification re-read 404s, helper no-ops.

    The T31b ticket: ``delete_page`` and ``move_page`` get a
    "lighter verification" because the source post-delete
    is gone. The helper's ``except PageNotFound: return``
    branch handles this — no spurious concurrency error. The
    test asserts the delete succeeds normally even when the
    re-read would 404.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE":
            return httpx.Response(200, headers={"ETag": '"v1"'})
        # Verification GET returns 404 (source is gone).
        return httpx.Response(404, text="not found")

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "delete_page", {"name": "index.md", "if_match": '"v1"'}
        )

    assert result.is_error is False# --- T32: create_page --------------------------------------------------


@pytest.mark.asyncio
async def test_create_page_returns_ack_envelope_on_200() -> None:
    """``create_page`` happy path returns the T23 ack envelope.

    Locks the wire shape: the agent that creates a page gets the
    same `{name, etag, size_bytes, last_modified_ms, created_ms}`
    envelope `write_page` returns, so a caller that learns one
    shape has it for both tools. The body is omitted (writes
    return meta only — T23).
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={
                "ETag": '"abc123"',
                "X-Last-Modified": "1700000000123",
                "X-Created": "1700000000000",
                "X-Content-Length": "4",
            },
        )

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "create_page", {"name": "new-page.md", "content": "body"}
        )

    assert result.is_error is False
    payload = result.structured_content or {}
    assert payload["name"] == "new-page.md"
    assert payload["etag"] == '"abc123"'
    assert payload["size_bytes"] == 4
    assert payload["last_modified_ms"] == 1700000000123
    assert payload["created_ms"] == 1700000000000


@pytest.mark.asyncio
async def test_create_page_sends_if_match_star_to_sb() -> None:
    """``create_page`` always sends ``If-Match: *`` (refuse to overwrite).

    Locks the implementation contract: ``create_page`` delegates
    to ``write_page(if_match="*")`, so the bridge sends
    ``If-Match: *`` to SB on every create. The caller never has
    to pass `if_match` (it's implied per the T32 ticket); the
    tool exists precisely because the agent shouldn't have to
    think about the precondition.
    """
    captured_if_match: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_if_match.append(request.headers.get("If-Match", ""))
        return httpx.Response(200, headers={"ETag": '"v1"'})

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        await client.call_tool(
            "create_page", {"name": "new.md", "content": "body"}
        )

    assert captured_if_match == ["*"]


@pytest.mark.asyncio
async def test_create_page_already_exists_translates_412_to_tool_error() -> None:
    """SB returning 412 (page exists) surfaces as ``page already exists: {name}; use write_page to overwrite``.

    The T32 charter's headline: the agent that calls
    ``create_page`` on an existing page gets a clean
    ``ToolError("page already exists: {name}; use write_page
    to overwrite")`` rather than the generic 412 wording
    (`"precondition failed; check if_match/if_none_match"`)
    that ``write_page`` would surface. The error names the
    page (so the agent can see what collided) AND names the
    right next tool (so the agent doesn't have to remember
    the pattern-match).
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(412, text="precondition failed")

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "create_page", {"name": "existing.md", "content": "body"}
        )

    assert result.is_error is True
    text = _text(result)
    assert "page already exists" in text
    assert "existing" in text
    assert "write_page" in text
    # The wording is *not* the generic 412 from
    # ``_translate_sb_errors`` (which would say
    # ``"precondition failed; check if_match/if_none_match"``).
    # This is the T32 translation.
    assert "precondition failed" not in text


@pytest.mark.asyncio
async def test_create_page_empty_name_returns_tool_error() -> None:
    """Empty ``name`` → upfront ``ToolError("name must not be empty")``.

    Cheap, no-read input validation first: an empty name is
    almost certainly a caller bug (the caller forgot to fill
    in the page name); surface it loudly before the round
    trip. Mirrors the upfront guards on the other tools
    (``text must not be empty``, ``find must not be empty``,
    ``ref must not be empty``). Locks the T32 charter's
    "empty `name` (upfront `ToolError`)" requirement.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        # No PUT should fire — the upfront guard rejects the
        # call before any SB round trip.
        return httpx.Response(200, headers={"ETag": '"v1"'})

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "create_page", {"name": "", "content": "body"}
        )

    assert result.is_error is True
    assert "name must not be empty" in _text(result)


@pytest.mark.asyncio
async def test_create_page_whitespace_only_name_returns_tool_error() -> None:
    """``"   "`` → upfront ``ToolError("name must not be empty")``.

    Whitespace-only names are empty in practice (SB would
    reject the request downstream, but the bridge catches it
    upstream). Same surface as the empty-name case so the
    agent sees one shape across both.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"ETag": '"v1"'})

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "create_page", {"name": "   \n  ", "content": "body"}
        )

    assert result.is_error is True
    assert "name must not be empty" in _text(result)


@pytest.mark.asyncio
async def test_create_page_404_does_not_silently_succeed() -> None:
    """A 404 on create (theoretically impossible since we're *creating*) still surfaces.

    The :func:`_translate_sb_errors` helper's 404 clause is
    reached when SB returns 404. SB's ``/.fs`` PUT handler
    doesn't typically 404 (the create-or-overwrite shape is
    the whole point of the endpoint), but if it ever does,
    the agent sees a ``page not found`` wording rather than
    a silent success. Locks the "no hidden failures"
    invariant.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="page not found")

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "create_page", {"name": "anything.md", "content": "body"}
        )

    assert result.is_error is True
    assert "page not found" in _text(result)


@pytest.mark.asyncio
async def test_create_page_5xx_returns_tool_error() -> None:
    """SB 5xx on a create → ``ToolError``, not a silent success.

    The wording matches the design doc § Tools § Status-code
    mapping: 5xx becomes ``silverbullet error: {status}`` (the
    same wording every other SB-backed tool uses). A future
    ticket could thread the upstream error text through
    ``ServerError``, but that's a separate concern; the T32
    charter is to wire ``create_page`` through the standard
    error-translation path, not to redesign the error
    envelope.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="upstream gone")

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "create_page", {"name": "anything.md", "content": "body"}
        )

    assert result.is_error is True
    assert "silverbullet error: 503" in _text(result)


@pytest.mark.asyncio
async def test_create_page_does_not_expose_if_match_in_tool_schema() -> None:
    """``create_page``'s tool schema omits ``if_match`` (implied ``"*"``).

    The T32 charter: ``if_match="*"`` is implied (no need to
    make the caller pass it). The MCP tool schema exposes only
    ``name`` and ``content`` as caller-facing arguments —
    threading an etag precondition would be a misuse of the
    create semantic (``write_page(if_match=<etag>)`` is the
    right tool for that). This test introspects the schema via
    ``list_tools`` and asserts ``create_page``'s
    ``inputSchema`` does not declare ``if_match`` as a
    property, so an agent reading the schema can't
    accidentally try to pass one.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"ETag": '"v1"'})

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        tools = await client.list_tools()
        create_page_tool = next(
            t for t in tools.tools if t.name == "create_page"
        )
        schema = create_page_tool.input_schema
        properties = schema.get("properties", {})
        assert "name" in properties
        assert "content" in properties
        # ``if_match`` is implied — not exposed to callers.
        # A future T32a could add an ``overwrite=False`` knob
        # if a use case appears, but the charter is "create
        # or refuse" with no caller-controlled precondition.
        assert "if_match" not in properties# --- T33: prepend_to_page --------------------------------------------


@pytest.mark.asyncio
async def test_prepend_to_page_happy_path_no_frontmatter() -> None:
    """``prepend_to_page`` on a body without frontmatter lands content at the absolute top.

    The simplest case: a page with no ``---\\n…\\n---\\n`` block,
    default ``position="after_frontmatter"`` (which behaves
    identically to ``"top"`` when there's no frontmatter to
    anchor against). The new content lands at the absolute top
    of the page body.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text="hello world\n")
        return httpx.Response(200, headers={"ETag": '"v2"'})

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "prepend_to_page",
            {"name": "index.md", "content": "HEADER\n"},
        )

    assert result.is_error is False
    payload = result.structured_content or {}
    assert payload["name"] == "index.md"


@pytest.mark.asyncio
async def test_prepend_to_page_default_after_frontmatter_inserts_below_block() -> None:
    """Default ``position="after_frontmatter"`` puts content *below* the YAML block.

    The human-meaningful default: a page with a YAML
    frontmatter block gets the new content *between* the
    closing ``---`` and the first body line, NOT above the
    opening ``---`` (which would break frontmatter consumers
    that expect to find the block at the top of the page).
    """
    original_body = "---\ntitle: My Page\ntags: [foo]\n---\nbody line\n"
    new_content = "INSERTED\n"

    captured_writes: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text=original_body)
        if request.method == "PUT":
            captured_writes.append(request.content.decode("utf-8"))
            return httpx.Response(200, headers={"ETag": '"v2"'})
        return httpx.Response(200, headers={"ETag": '"v2"'})

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "prepend_to_page",
            {"name": "index.md", "content": new_content},
        )

    assert result.is_error is False
    assert captured_writes == [
        "---\ntitle: My Page\ntags: [foo]\n---\nINSERTED\nbody line\n"
    ]


@pytest.mark.asyncio
async def test_prepend_to_page_position_top_inserts_above_frontmatter() -> None:
    """``position="top"`` puts the new content above the YAML block.

    The override path: the caller explicitly wants to push
    the frontmatter down (rare; almost always a bug in
    practice, but a.md legitimate intent the tool exposes).
    The new content lands at the absolute top of the file,
    above the opening ``---`` fence.
    """
    original_body = "---\ntitle: My Page\n---\nbody line\n"
    new_content = "BEFORE FM\n"

    captured_writes: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text=original_body)
        if request.method == "PUT":
            captured_writes.append(request.content.decode("utf-8"))
            return httpx.Response(200, headers={"ETag": '"v2"'})
        return httpx.Response(200, headers={"ETag": '"v2"'})

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "prepend_to_page",
            {
                "name": "index.md",
                "content": new_content,
                "position": "top",
            },
        )

    assert result.is_error is False
    assert captured_writes == [
        "BEFORE FM\n---\ntitle: My Page\n---\nbody line\n"
    ]


@pytest.mark.asyncio
async def test_prepend_to_page_position_top_without_frontmatter() -> None:
    """``position="top"`` on a body without frontmatter = ``"after_frontmatter"``.

    Without frontmatter to anchor against, both positions
    produce the same splice (new content at absolute top).
    Locks the "no frontmatter to push down" equivalence.
    """
    captured_writes: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text="body line\n")
        if request.method == "PUT":
            captured_writes.append(request.content.decode("utf-8"))
            return httpx.Response(200, headers={"ETag": '"v2"'})
        return httpx.Response(200, headers={"ETag": '"v2"'})

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "prepend_to_page",
            {
                "name": "index.md",
                "content": "HEADER\n",
                "position": "top",
            },
        )

    assert result.is_error is False
    assert captured_writes == ["HEADER\nbody line\n"]


@pytest.mark.asyncio
async def test_prepend_to_page_malformed_frontmatter_treated_as_no_frontmatter() -> None:
    """A page that opens with ``---`` but doesn't close it = no-frontmatter.

    The T33 ticket's explicit rule: a malformed frontmatter
    block is treated as no-frontmatter at all. The new
    content lands at the absolute top, same as a page
    with no ``---`` opening fence. Locks the "raw text,
    no parser" stance from the ticket.
    """
    original_body = "---orphan body line\n"  # opening fence, no closing
    new_content = "HEADER\n"

    captured_writes: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text=original_body)
        if request.method == "PUT":
            captured_writes.append(request.content.decode("utf-8"))
            return httpx.Response(200, headers={"ETag": '"v2"'})
        return httpx.Response(200, headers={"ETag": '"v2"'})

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "prepend_to_page",
            {"name": "index.md", "content": new_content},
        )

    assert result.is_error is False
    # New content at absolute top — frontmatter-less splice.
    assert captured_writes == ["HEADER\n---orphan body line\n"]


@pytest.mark.asyncio
async def test_prepend_to_page_empty_content_returns_tool_error() -> None:
    """Empty ``content`` → upfront ``ToolError("content must not be empty")``.

    Mirrors the empty-``text`` guard on ``append_to_page``.
    Surface it loudly before the read-modify-write round
    trip; an empty prepend is almost certainly a caller bug.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        # No GET expected — the upfront guard rejects the
        # call before any SB round trip.
        return httpx.Response(200, headers={"ETag": '"v2"'})

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "prepend_to_page",
            {"name": "index.md", "content": ""},
        )

    assert result.is_error is True
    assert "content must not be empty" in _text(result)


@pytest.mark.asyncio
async def test_prepend_to_page_unknown_position_returns_tool_error() -> None:
    """Unknown ``position`` → upfront ``ToolError("position must be one of: …")`.

    Same upfront-rejection pattern as the empty-content
    guard. The two-mode ``position`` knob is the
    frontmatter-aware default + the absolute-top override;
    anything else is a typo (``"topmost"``, ``"first"``,
    ``"above_frontmatter"``, ...) and should surface
    loudly before any FS walk.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"ETag": '"v2"'})

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "prepend_to_page",
            {
                "name": "index.md",
                "content": "body",
                "position": "topmost",
            },
        )

    assert result.is_error is True
    assert "position must be one of" in _text(result)
    assert "after_frontmatter" in _text(result)
    assert "top" in _text(result)


@pytest.mark.asyncio
async def test_prepend_to_page_dry_run_returns_preview_without_writing() -> None:
    """``dry_run=True`` returns the T26 preview envelope without writing.

    Mirrors the dry-run behavior on ``append_to_page`` /
    ``patch_page_lines`` / ``patch_page_replace``. The
    dry-run envelope surfaces the *post-shaping* body
    (with frontmatter-aware splice applied), so the diff
    the agent sees is exactly what would have been
    written.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text="body line\n")
        # No PUT expected on a dry-run path.
        return httpx.Response(200, headers={"ETag": '"v2"'})

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "prepend_to_page",
            {
                "name": "index.md",
                "content": "HEADER\n",
                "dry_run": True,
            },
        )

    assert result.is_error is False
    payload = result.structured_content or {}
    assert payload["dry_run"] is True
    assert payload["original"] == "body line\n"
    assert payload["patched"] == "HEADER\nbody line\n"
    # Diff is a non-empty string for any non-trivial patch;
    # we don't pin the exact wording (difflib formatting
    # can drift across Python versions) — just that the
    # field is populated.
    assert payload["diff"]


@pytest.mark.asyncio
async def test_prepend_to_page_dry_run_does_not_invoke_t31b_verification() -> None:
    """``dry_run=True`` short-circuits T31b's post-write verification helper.

    Locks the T31b helper's ``dry_run=True`` opt-out for
    the new tool — a regression that fired the verification
    path on dry-run would turn every dry-run into a
    concurrency check against the pre-write read, which is
    meaningless (there's no PUT to race against).
    """
    def handler(request: httpx.Request) -> httpx.Response:
        # GET returns a different etag on every read; if
        # T31b fired, the second GET's etag would differ
        # from the first and the helper would raise
        # ``concurrent edit detected``. Since dry_run
        # short-circuits, only one GET (the read) should
        # fire.
        if request.method == "GET":
            return httpx.Response(
                200, text="body line\n", headers={"ETag": '"v1"'}
            )
        return httpx.Response(200, headers={"ETag": '"v1"'})

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "prepend_to_page",
            {
                "name": "index.md",
                "content": "HEADER\n",
                "if_match": '"v1"',
                "dry_run": True,
            },
        )

    assert result.is_error is False
    payload = result.structured_content or {}
    assert payload["dry_run"] is True


@pytest.mark.asyncio
async def test_prepend_to_page_auto_threads_read_etag_when_if_match_is_none() -> None:
    """``if_match=None`` auto-threads the read's etag into the write's ``If-Match``.

    Same auto-thread pattern as ``append_to_page`` / the
    patch tools: when the caller passes ``None``, the
    read's etag is threaded into the write's precondition
    so a concurrent edit between read and write surfaces
    as ``concurrent edit detected`` via the T31b helper,
    even without the caller managing an etag round-trip.
    """
    request_count = {"get": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            request_count["get"] += 1
            if request_count["get"] == 1:
                # First read: returns the read's etag,
                # which the bridge auto-threads into the
                # write's ``If-Match``.
                return httpx.Response(
                    200, text="body\n", headers={"ETag": '"read-etag"'}
                )
            # Verification GET returns a drifted etag.
            return httpx.Response(
                200, text="body\n", headers={"ETag": '"new"'}
            )
        return httpx.Response(200, headers={"ETag": '"new"'})

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "prepend_to_page",
            {"name": "index.md", "content": "HEADER\n"},
        )

    assert result.is_error is True
    assert "concurrent edit detected" in _text(result)


@pytest.mark.asyncio
async def test_prepend_to_page_404_returns_tool_error() -> None:
    """Missing page → standard ``ToolError("page not found: {name}")`` wording."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="page not found")

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "prepend_to_page",
            {"name": "missing.md", "content": "body"},
        )

    assert result.is_error is True
    assert "page not found" in _text(result)
    assert "missing" in _text(result)


@pytest.mark.asyncio
async def test_prepend_to_page_412_returns_tool_error() -> None:
    """Stale ``if_match`` → standard 412 wording (``"precondition failed; …"``)."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(412, text="precondition failed")

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "prepend_to_page",
            {"name": "index.md", "content": "body", "if_match": '"v0"'},
        )

    assert result.is_error is True
    assert "precondition failed" in _text(result)

# --- _split_frontmatter_block helper unit tests (T33) ------------------


def test_split_frontmatter_block_no_frontmatter_returns_none() -> None:
    """Body without a leading ``---`` fence → ``(None, body)``.

    The canonical "no frontmatter" signal: ``None``, matching
    the journal helper's contract. Callers that don't care
    about the no-frontmatter distinction can treat ``None``
    the same as ``[]`` and not break; :func:`prepend_to_page`
    treats them identically (the splice is the same).
    """
    from mcp_silverbullet.server import _split_frontmatter_block

    fm, rest = _split_frontmatter_block("body content\n")
    assert fm is None
    assert rest == "body content\n"


def test_split_frontmatter_block_with_frontmatter_splits_correctly() -> None:
    """Body with a leading ``---\\n…\\n---\\n`` block → ``(fm, rest)``.

    The wire shape: ``frontmatter`` carries the opening
    fence, the ``…`` lines, the closing fence, AND the
    trailing ``\\n`` after the closing fence (so
    concatenation is correct: ``fm + content + rest``). The
    ``rest`` half starts at the line *after* the closing
    fence with its trailing newline preserved iff the
    original body had one.
    """
    from mcp_silverbullet.server import _split_frontmatter_block

    body = "---\ntitle: My Page\ntags: [foo]\n---\nbody line\n"
    fm, rest = _split_frontmatter_block(body)
    # Frontmatter carries the opening fence through the
    # trailing newline after the closing fence.
    assert fm == "---\ntitle: My Page\ntags: [foo]\n---\n"
    # Body half starts at the line after the closing fence
    # and preserves the trailing newline.
    assert rest == "body line\n"


def test_split_frontmatter_block_malformed_returns_none() -> None:
    """Body that opens with ``---`` but doesn't close it → ``(None, body)``.

    Locks the T33 ticket's "raw text, no parser" stance:
    a malformed frontmatter block is treated as no
    frontmatter at all. The body shape stays the same
    (the tool doesn't have to special-case a malformed
    page) and the no-frontmatter signal is honest about
    the page being broken.
    """
    from mcp_silverbullet.server import _split_frontmatter_block

    # Opening fence, no closing fence.
    fm, rest = _split_frontmatter_block("---orphan body line\n")
    assert fm is None
    assert rest == "---orphan body line\n"


def test_split_frontmatter_block_empty_body_returns_none() -> None:
    """Empty body → ``(None, "")``.

    Defensive: an empty body is the trivial no-frontmatter
    case. Locks the helper's behavior on the edge input
    (the journal helper's splitlines-based path returns
    ``[]`` on empty input; this one matches).
    """
    from mcp_silverbullet.server import _split_frontmatter_block

    fm, rest = _split_frontmatter_block("")
    assert fm is None
    assert rest == ""


def test_split_frontmatter_block_body_without_trailing_newline() -> None:
    """Body without a trailing newline preserves the no-newline shape.

    The body half (``rest``) doesn't gain a trailing
    newline it didn't have. Without this invariant, a
    prepended page that originally ended without a newline
    would gain one — silently changing the page's wire
    shape and potentially breaking downstream consumers
    that compare body bytes.
    """
    from mcp_silverbullet.server import _split_frontmatter_block

    body = "---\ntitle: My Page\n---\nbody without trailing newline"
    fm, rest = _split_frontmatter_block(body)
    # The closing fence was followed by a newline in the
    # original body (every well-formed frontmatter block
    # ends with ``---\n``), so ``fm`` carries that
    # newline. The body half starts immediately after
    # the newline.
    assert fm == "---\ntitle: My Page\n---\n"
    assert rest == "body without trailing newline"
    assert not rest.endswith("\n")


# --- T36: 256 KiB body-size cap ----------------------------------------


# Helper bodies for cap tests — exactly-at-cap (256 KiB) and
# over-the-cap (256 KiB + 1 byte). Allocating as ``bytes`` is
# the cleanest way to land an exact size without counting
# individual codepoints.
CAP_BODY = "a" * (256 * 1024)
OVER_CAP_BODY = "a" * (256 * 1024 + 1)


def _ok_handler() -> "callable":
    """A handler that returns 200 for every method (used for cap tests)."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"ETag": '"v1"'})
    return handler


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool_name,args",
    [
        ("write_page", {"name": "x.md", "content": OVER_CAP_BODY}),
        ("create_page", {"name": "x.md", "content": OVER_CAP_BODY}),
        ("append_to_page", {"name": "x.md", "text": OVER_CAP_BODY}),
        ("prepend_to_page", {"name": "x.md", "content": OVER_CAP_BODY}),
        (
            "patch_page_lines",
            {
                "name": "x.md",
                "start_line": 1,
                "end_line": 1,
                "new_content": OVER_CAP_BODY,
            },
        ),
        (
            "patch_page_replace",
            {"name": "x.md", "find": "x", "new_string": OVER_CAP_BODY},
        ),
    ],
)
async def test_t36_body_cap_fires_on_every_write_tool(
    tool_name: str, args: dict
) -> None:
    """A 257 KiB body raises ``body too large`` on every write tool.

    The T36 ticket's "Done when" calls for a parametrized test
    (one per tool, all asserting the same ``body too large``
    wording). The cap is enforced at the tool-handler level
    — the SB round trip never happens on an oversized body,
    so any 200 response on these tools with a 257 KiB input
    is a regression.
    """
    server = _build(_ok_handler())
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(tool_name, args)

    assert result.is_error is True
    text = _text(result)
    assert "body too large" in text
    # The error names the size, the cap, and the remediation
    # hint — ``xmatthewx``-style. The exact size and cap
    # values are spelled out so the agent can act on them
    # without guessing.
    assert str(256 * 1024 + 1) in text
    assert "256 KiB" in text
    assert "append_to_page" in text


@pytest.mark.asyncio
async def test_t36_body_cap_accepts_exact_cap_boundary() -> None:
    """A body of *exactly* 256 KiB passes the cap (boundary, inclusive).

    Locks the inclusive-cap invariant: ``size > cap`` rejects,
    ``size <= cap`` accepts. The boundary case is the one
    most likely to drift across refactors; an off-by-one
    here would surface as either a 200 with a too-large
    body or a ``body too large`` with a legal-sized body,
    both regressions the agent would notice.
    """
    server = _build(_ok_handler())
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "write_page", {"name": "x.md", "content": CAP_BODY}
        )

    assert result.is_error is False


@pytest.mark.asyncio
async def test_t36_body_cap_fires_before_sb_round_trip() -> None:
    """The cap fires *before* any PUT — the SB handler is never called.

    Verifies the T36 charter's "before the SB round trip"
    constraint. A test handler that records whether it saw
    a PUT is the right shape: the handler's PUT counter
    must stay at zero when the cap fires.
    """
    put_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT":
            put_count["n"] += 1
        return httpx.Response(200, headers={"ETag": '"v1"'})

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "write_page", {"name": "x.md", "content": OVER_CAP_BODY}
        )

    assert result.is_error is True
    assert "body too large" in _text(result)
    assert put_count["n"] == 0


@pytest.mark.asyncio
async def test_t36_body_cap_does_not_apply_to_read_page() -> None:
    """``read_page`` is unaffected by the cap (read-side, not write-side).

    The T36 ticket is explicit: the cap applies to every
    *write* tool. ``read_page``'s body can be arbitrarily
    large; the agent that wants to chunk a big page
    reads it (no cap) and ``append_to_page`` chunks (capped
    per chunk). Locks the "read-side is unaffected"
    invariant.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        # 500 KiB body — way over the cap. ``read_page``
        # must surface it without raising.
        return httpx.Response(
            200, text="a" * (500 * 1024), headers={"ETag": '"v1"'}
        )

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("read_page", {"name": "x.md"})

    assert result.is_error is False


@pytest.mark.asyncio
async def test_t36_body_cap_does_not_apply_to_list_pages() -> None:
    """``list_pages`` is unaffected by the cap (no body to cap).

    The T36 ticket: ``list_pages`` returns metadata, not
    bodies. No cap applies. Locks the read-side invariant
    for ``list_pages`` specifically (in case a future
    ticket wants to widen the cap to cover response-side
    payloads, the test would surface the regression).
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text=json.dumps(
                [
                    {"name": f"page-{i}", "size": 1000000}
                    for i in range(100)
                ]
            ),
            headers={"Content-Type": "application/json"},
        )

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("list_pages", {})

    assert result.is_error is False


@pytest.mark.asyncio
async def test_t36_body_cap_does_not_apply_to_page_exists() -> None:
    """``page_exists`` is unaffected by the cap (no body to cap).

    ``page_exists`` is a cheap GET that returns a bool; no
    body bytes pass through the bridge. The cap doesn't
    apply. Locks the read-side invariant for ``page_exists``.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="body", headers={"ETag": '"v1"'})

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("page_exists", {"name": "x.md"})

    assert result.is_error is False


@pytest.mark.asyncio
async def test_t36_body_cap_does_not_apply_to_diff_pages() -> None:
    """``diff_pages`` is unaffected by the cap (read-only, returns a diff string).

    ``diff_pages`` reads two pages and returns a unified
    diff — it's a read-side tool, not a write. The cap
    doesn't apply. (A diff could in theory exceed the cap
    on pathological inputs, but that's a separate concern
    from the write-side guardrail.)
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="body", headers={"ETag": '"v1"'})

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "diff_pages",
            {"name": "x.md", "other_body": "other"},
        )

    assert result.is_error is False


@pytest.mark.asyncio
async def test_t36_body_cap_fires_on_move_page_when_source_body_is_oversized() -> None:
    """``move_page``'s cap fires when the *source body* exceeds 256 KiB.

    ``move_page`` is unique among the write tools: the
    caller's "body" is the source page's stored body, which
    the bridge reads and re-writes at the destination. The
    cap applies to that about-to-be-written body (matching
    the T36 charter's "the body the bridge is about to
    write" wording). A 600 KB source page moving to a new
    name surfaces the same ``body too large`` error the
    other write tools would.
    """
    oversized_source = "a" * (256 * 1024 + 1)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200, text=oversized_source, headers={"ETag": '"v1"'}
            )
        return httpx.Response(200, headers={"ETag": '"v1"'})

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "move_page", {"name": "src.md", "new_name": "dst.md"}
        )

    assert result.is_error is True
    assert "body too large" in _text(result)


@pytest.mark.asyncio
async def test_t36_body_cap_fires_on_check_task_when_page_body_is_oversized() -> None:
    """``check_task``'s cap fires when the post-shaping body exceeds 256 KiB.

    ``check_task`` is a single-character edit; the
    post-shaping body is roughly the same size as the
    pre-shaping body. The cap applies to the post-shaping
    body (what the PUT will carry), so ``check_task`` on a
    > 256 KiB page surfaces the same ``body too large``
    error the other write tools would.
    """
    oversized_page = "a" * (256 * 1024 + 1) + "\n- [ ] [[Ref]] bullet\n"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200, text=oversized_page, headers={"ETag": '"v1"'}
            )
        return httpx.Response(200, headers={"ETag": '"v1"'})

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "check_task", {"page": "x", "ref": "Ref"}
        )

    assert result.is_error is True
    assert "body too large" in _text(result)


@pytest.mark.asyncio
async def test_t36_body_cap_dry_run_still_fires() -> None:
    """``dry_run=True`` doesn't bypass the cap (cap is on the caller's body, not the post-shaping).

    The cap fires on the caller's body before any read or
    write happens. ``dry_run=True`` paths go through the
    same upfront guard. Locks the "cap is everywhere the
    cap is supposed to be" invariant — a regression that
    moved the cap check inside ``dry_run=False`` only would
    surface here.
    """
    server = _build(_ok_handler())
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "append_to_page",
            {
                "name": "x.md",
                "text": OVER_CAP_BODY,
                "dry_run": True,
            },
        )

    assert result.is_error is True
    assert "body too large" in _text(result)


def test_t36_check_body_size_accepts_empty_string() -> None:
    """An empty string is well under the cap (passes).

    Sanity check: the helper's ``len(body.encode("utf-8"))``
    measurement works on the empty-string edge case (size
    0, well under the 256 KiB cap). ``write_page(name, "")``
    would not raise the cap (it would succeed at writing an
    empty page). The helper itself is silent on success.
    """
    from mcp_silverbullet.server import _check_body_size

    # No exception.
    _check_body_size("")


def test_t36_check_body_size_accepts_boundary() -> None:
    """A body of *exactly* 256 KiB passes (boundary, inclusive).

    Pins the inclusive cap invariant at the helper level
    (the tool-level parametrized test covers the wire
    surface; this covers the helper directly so a
    regression in the boundary check itself — e.g., ``>``
    becoming ``>=`` — surfaces here without going through
    the MCP tool layer).
    """
    from mcp_silverbullet.server import _check_body_size

    # No exception at the boundary.
    _check_body_size("a" * (256 * 1024))


def test_t36_check_body_size_rejects_over_cap() -> None:
    """A body of 256 KiB + 1 byte raises ``body too large``.

    Pins the boundary at the helper level. The error
    message must name the size, the cap, and the
    remediation hint (``append_to_page`` chunks).
    """
    from mcp_silverbullet.server import _check_body_size

    with pytest.raises(ToolError) as excinfo:
        _check_body_size("a" * (256 * 1024 + 1))

    text = str(excinfo.value)
    assert "body too large" in text
    assert str(256 * 1024 + 1) in text
    assert "256 KiB" in text
    assert "append_to_page" in text


def test_t36_check_body_size_uses_utf8_byte_count_not_codepoint_count() -> None:
    """Multi-byte characters count as their UTF-8 byte count, not codepoints.

    ``"é"`` is 1 codepoint but 2 UTF-8 bytes. A body of
    256 KiB of ``"é"`` (128 Ki codepoints, 256 KiB bytes)
    passes; a body of 256 KiB + 1 byte of ``"é"`` fails.
    Locks the UTF-8 byte-count invariant from the T36
    ticket — the agent that computes ``len(body)`` (Python
    codepoint count) would underestimate; the bridge
    measures ``len(body.encode("utf-8"))``.
    """
    from mcp_silverbullet.server import _check_body_size

    # 256 KiB *of bytes* = 128 Ki * of codepoints * 2 bytes each.
    boundary = "é" * (128 * 1024)
    assert len(boundary.encode("utf-8")) == 256 * 1024
    _check_body_size(boundary)  # boundary, no raise

    over = boundary + "é"
    assert len(over.encode("utf-8")) == 256 * 1024 + 2
    with pytest.raises(ToolError) as excinfo:
        _check_body_size(over)
    assert "body too large" in str(excinfo.value)


# --- T40: lift upfront empty-input validation across write tools ------
#
# The bug report's b9 surfaces a real gap on the v1.3 code: four
# write tools (``write_page`` / ``delete_page`` / ``move_page`` /
# ``patch_page_lines``) had no upfront empty-input guard, so
# ``write_page(name="", content="test")`` and ``write_page(name="x",
# content="")`` both reached SB and surfaced a 500. T40 lifts the
# pattern from the already-guarded tools (``create_page` /
# ``append_to_page`` / ``prepend_to_page`` / ``patch_page_replace``
# / ``check_task``) into two shared helpers
# (:func:`_validate_nonempty_name` / :func:`_validate_nonempty_value`)
# and threads them into the un-guarded tools at the top of each
# handler — *before* T39's name normalization so a caller passing
# ``name=""`` still sees the loud empty-name error rather than the
# normalized form ``".md"`` silently succeeding.
#
# These tests lock the T40 charter: empty inputs surface
# :exc:`ToolError` upfront with no SB round trip (the handler
# never fires, so the mock's PUT counter stays at zero), and the
# wording matches the existing inline guards exactly so agents
# that have learned the shape for one tool see the same shape
# across all of them.


@pytest.mark.asyncio
async def test_t40_write_page_empty_name_returns_tool_error() -> None:
    """``write_page(name="")`` → upfront ``ToolError("name must not be empty")``.

    The guard fires before any SB round trip, before
    :func:`_normalize_page_name`, and before
    :func:`_check_body_size`. The agent sees the same wording as
    :func:`create_page` so the surface is consistent across the
    create-vs-overwrite split.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"ETag": '"v1"'})

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "write_page", {"name": "", "content": "hello"}
        )

    assert result.is_error is True
    assert "name must not be empty" in _text(result)


@pytest.mark.asyncio
async def test_t40_write_page_whitespace_only_name_returns_tool_error() -> None:
    """``write_page(name="  \\n  ")`` → upfront ``ToolError("name must not be empty")``.

    Whitespace-only names are empty in practice — SB would reject
    them downstream with a less-helpful 500. The guard catches it
    upstream with the same wording so the agent sees one shape
    across the empty and whitespace-only cases.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"ETag": '"v1"'})

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "write_page",
            {"name": "  \n  ", "content": "hello"},
        )

    assert result.is_error is True
    assert "name must not be empty" in _text(result)


@pytest.mark.asyncio
async def test_t40_write_page_empty_content_returns_tool_error() -> None:
    """``write_page(content="")`` → upfront ``ToolError("content must not be empty")``.

    A zero-byte overwrite is almost certainly a caller bug
    (``write_page` is overwrite-or-create; for an empty body the
    caller wants ``delete_page``). The guard fires before any
    SB round trip. Wording matches :func:`prepend_to_page`'s
    existing ``content must not be empty`` style.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"ETag": '"v1"'})

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "write_page", {"name": "Foo", "content": ""}
        )

    assert result.is_error is True
    assert "content must not be empty" in _text(result)


@pytest.mark.asyncio
async def test_t40_write_page_whitespace_only_content_returns_tool_error() -> None:
    """``write_page(content="  ")`` → upfront ``ToolError("content must not be empty")``.

    Whitespace-only content is empty in practice. Same wording
    as the empty case so the agent sees one shape across both.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"ETag": '"v1"'})

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "write_page",
            {"name": "Foo", "content": "  \t\n"},
        )

    assert result.is_error is True
    assert "content must not be empty" in _text(result)


@pytest.mark.asyncio
async def test_t40_write_page_no_sb_round_trip_on_empty_inputs() -> None:
    """Empty inputs surface the guard before any PUT fires.

    A mock that 500s on PUT would otherwise surface a
    ``silverbullet error: 500`` rather than the empty-input
    guard — locking the no-round-trip invariant catches a
    regression that moves the guard below the SB call.
    """
    seen_puts: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT":
            seen_puts.append(request.content)
        # Return 500 on PUT so a regression surfaces as a
        # ``silverbullet error: 500`` rather than silent success.
        return httpx.Response(500, text="boom")

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        for empty in ({"name": "", "content": "x"}, {"name": "x", "content": ""}):
            result = await client.call_tool("write_page", empty)
            assert result.is_error is True
            assert "must not be empty" in _text(result)

    assert seen_puts == []


@pytest.mark.asyncio
async def test_t40_delete_page_empty_name_returns_tool_error() -> None:
    """``delete_page(name="")`` → upfront ``ToolError("name must not be empty")``.

    Same wording as :func:`write_page` / :func:`create_page` so
    the agent sees one shape across all ``name``-taking tools.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"ETag": '"v1"'})

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "delete_page", {"name": ""}
        )

    assert result.is_error is True
    assert "name must not be empty" in _text(result)


@pytest.mark.asyncio
async def test_t40_move_page_empty_source_name_returns_tool_error() -> None:
    """``move_page(name="", new_name="Foo")`` → upfront ``ToolError("name must not be empty")``.

    The guard fires on the source ``name`` before any SB round
    trip. Wording matches the other ``name``-taking tools.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"ETag": '"v1"'})

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "move_page",
            {"name": "", "new_name": "Foo"},
        )

    assert result.is_error is True
    assert "name must not be empty" in _text(result)


@pytest.mark.asyncio
async def test_t40_move_page_empty_destination_name_returns_tool_error() -> None:
    """``move_page(name="Foo", new_name="")`` → upfront ``ToolError("name must not be empty")``.

    The guard fires on the destination ``new_name`` before any
    SB round trip. Both ``name`` and ``new_name`` are guarded
    by the same helper so the wording matches.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"ETag": '"v1"'})

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "move_page",
            {"name": "Foo", "new_name": ""},
        )

    assert result.is_error is True
    assert "name must not be empty" in _text(result)


@pytest.mark.asyncio
async def test_t40_move_page_both_empty_returns_tool_error() -> None:
    """``move_page(name="", new_name="")`` → upfront ``ToolError("name must not be empty")``.

    Both guards fire (the source guard runs first); the agent
    sees one error message rather than two. Whichever the source
    guard surfaces, the wording is consistent with the rest of
    the bridge.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"ETag": '"v1"'})

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "move_page",
            {"name": "", "new_name": ""},
        )

    assert result.is_error is True
    assert "name must not be empty" in _text(result)


@pytest.mark.asyncio
async def test_t40_patch_page_lines_empty_name_returns_tool_error() -> None:
    """``patch_page_lines(name="", ...)`` → upfront ``ToolError("name must not be empty")``.

    The guard fires before the ``start_line`` / ``end_line`` /
    ``new_content`` checks (a caller passing ``name=""`` plus
    out-of-bounds line numbers would otherwise see the line-
    range error first, which is misleading). Wording matches
    the other ``name``-taking tools.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"ETag": '"v1"'})

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "patch_page_lines",
            {
                "name": "",
                "start_line": 1,
                "end_line": 1,
                "new_content": "x",
            },
        )

    assert result.is_error is True
    assert "name must not be empty" in _text(result)


@pytest.mark.asyncio
async def test_t40_patch_page_replace_empty_new_string_does_NOT_raise() -> None:
    """``patch_page_replace(new_string="")`` is the documented "delete match" path.

    T40's charter originally proposed guarding ``new_string``
    too, but the surface explicitly documents
    ``new_string=""`` as "delete every match"
    (``"abcdefg".replace("cd", "")`` is ``"abefg"``). The
    ticket's intent was the four tools with no guard at all;
    :func:`patch_page_replace` already has the ``find`` guard
    (the half that prevents a runaway match-everywhere), and
    the ``new_string`` empty case is a legitimate edit, not a
    caller bug. This test locks the *absence* of the guard so
    a future "consistency" change doesn't accidentally
    regress the documented delete-match path.
    """
    seen_writes: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text="abcdefg")
        seen_writes.append(request.content)
        return httpx.Response(200, headers={"ETag": '"v2"'})

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "patch_page_replace",
            {
                "name": "index.md",
                "find": "cd",
                "new_string": "",
            },
        )

    assert result.is_error is False
    # ``cd`` is a single match → ``replace_all=False`` succeeds
    # with a deletion. The PUT writes ``"abefg"``.
    assert seen_writes == [b"abefg"]


@pytest.mark.asyncio
async def test_t40_create_page_empty_name_still_uses_shared_helper() -> None:
    """``create_page(name="")`` → upfront ``ToolError("name must not be empty")``.

    The pre-existing inline guard on :func:`create_page` is
    replaced with :func:`_validate_nonempty_name` in T40 so the
    wording and shape stay consistent across all name-taking
    tools. This test pins the surface; the wording has not
    changed (``"name must not be empty"`` is exactly what the
    inline guard already said).
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"ETag": '"v1"'})

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "create_page", {"name": "", "content": "hello"}
        )

    assert result.is_error is True
    assert "name must not be empty" in _text(result)


@pytest.mark.asyncio
async def test_t40_check_task_empty_ref_still_uses_shared_helper() -> None:
    """``check_task(ref="")`` → upfront ``ToolError("ref must not be empty")``.

    The pre-existing inline guard on :func:`check_task` is
    replaced with :func:`_validate_nonempty_value(ref,
    label="ref")` in T40. The wording is unchanged; the test
    pins the surface so a future threading change can't
    silently rename ``ref`` to ``name`` (which would be wrong
    — ``ref`` is a wikilink target, not a page name).
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"ETag": '"v1"'})

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "check_task",
            {"page": "Foo", "ref": "", "state": "done"},
        )

    assert result.is_error is True
    assert "ref must not be empty" in _text(result)


@pytest.mark.asyncio
async def test_t40_append_to_page_empty_text_still_uses_shared_helper() -> None:
    """``append_to_page(text="")`` → upfront ``ToolError("text must not be empty")``.

    The pre-existing inline guard on :func:`append_to_page` is
    replaced with :func:`_validate_nonempty_value(text,
    label="text")` in T40. Wording unchanged; test pins the
    surface so a future threading change can't silently rename
    ``text`` to ``content``.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"ETag": '"v1"'})

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "append_to_page",
            {"name": "Foo", "text": ""},
        )

    assert result.is_error is True
    assert "text must not be empty" in _text(result)


@pytest.mark.asyncio
async def test_t40_prepend_to_page_empty_content_still_uses_shared_helper() -> None:
    """``prepend_to_page(content="")`` → upfront ``ToolError("content must not be empty")``.

    The pre-existing inline guard on :func:`prepend_to_page` is
    replaced with :func:`_validate_nonempty_value(content,
    label="content")` in T40. Wording unchanged; test pins the
    surface so a future threading change can't silently rename
    ``content`` to ``text`` (which would be wrong — the two
    tools' parameters are different).
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"ETag": '"v1"'})

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "prepend_to_page",
            {"name": "Foo", "content": ""},
        )

    assert result.is_error is True
    assert "content must not be empty" in _text(result)


@pytest.mark.asyncio
async def test_t40_patch_page_replace_empty_find_still_uses_shared_helper() -> None:
    """``patch_page_replace(find="")`` → upfront ``ToolError("find must not be empty")``.

    The pre-existing inline guard on :func:`patch_page_replace`
    is replaced with :func:`_validate_nonempty_value(find,
    label="find")` in T40. Wording unchanged; the guard still
    fires upfront so the runaway ``match between every char``
    case is rejected before any SB round trip.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"ETag": '"v1"'})

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "patch_page_replace",
            {
                "name": "Foo",
                "find": "",
                "new_string": "x",
            },
        )

    assert result.is_error is True
    assert "find must not be empty" in _text(result)


@pytest.mark.asyncio
async def test_t40_normalization_runs_after_empty_guard() -> None:
    """``write_page(name="", ...)`` → ``name must not be empty``, NOT ``".md"`` silently.

    Locks the T40 ↔ T39 ordering invariant: the empty-name guard
    fires *before* :func:`_normalize_page_name` so a caller
    passing ``name=""`` sees the loud empty-name error rather
    than the normalized form ``".md"`` silently succeeding
    (which would create a page named ``.md`` — definitely not
    what the caller meant).
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"ETag": '"v1"'})

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "write_page", {"name": "", "content": "hello"}
        )

    assert result.is_error is True
    text = _text(result)
    assert "name must not be empty" in text
    # The agent does *not* see the normalization-happened message
    # (``".md"`` was the resolved form), because the empty-name
    # guard fired first and raised before normalization could run.
    assert "normalized" not in text.lower()
    assert ".md" not in text


# --- v1.3 build-map invariants ----------------------------------------
# These tests pin the v1.3 destination against docstring drift. The
# ``MCPServer.instructions`` string is what the MCP client sees on
# ``initialize`` — a regression that drops a v1.3 tool name from the
# instructions while leaving the tool registered would be a
# silent-experience regression the agent would notice ("the tool
# is in the schema but not mentioned in the description? weird").
# Locking both shapes in a test prevents the drift.


@pytest.mark.asyncio
async def test_v1_3_instructions_advertise_fourteen_always_on_tools() -> None:
    """``instructions`` lists the 14 v1.3 always-on tools by name.

    v1.3 destination: the bridge exposes 14 ``/.fs``-backed +
    bullet-primitive tools (``read_page`` / ``page_exists`` /
    ``write_page`` / ``create_page`` / ``delete_page`` /
    ``append_to_page`` / ``prepend_to_page`` /
    ``patch_page_lines`` / ``patch_page_replace`` / ``move_page``
    / ``list_pages`` / ``diff_pages`` / ``check_task`` /
    ``list_tasks``) plus one resource template. A regression
    that drops a name from the instructions text (without
    dropping the corresponding tool) surfaces here as a
    failed ``assertIn``.

    The in-memory ``Client(mcp)`` doesn't expose ``initialize()``,
    so we read the ``MCPServer.instructions`` property directly —
    that's the same string the MCP SDK would send on the wire
    during a real ``initialize`` handshake (T5 of the prior map
    pins that round-trip).
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"ETag": '"v1"'})

    server = _build(handler)
    text = server.instructions
    assert text is not None
    for tool_name in (
        "read_page",
        "page_exists",
        "write_page",
        "create_page",
        "delete_page",
        "append_to_page",
        "prepend_to_page",
        "patch_page_lines",
        "patch_page_replace",
        "move_page",
        "list_pages",
        "diff_pages",
        "check_task",
        "list_tasks",
    ):
        assert tool_name in text, (
            f"v1.3 instructions missing tool name {tool_name!r}"
        )


@pytest.mark.asyncio
async def test_v1_3_instructions_advertise_six_journal_tools() -> None:
    """``instructions`` lists all 6 journal tools (T10–T12, T34, T35).

    v1.3 destination: the journal surface is 6 tools
    (``journal_histogram`` / ``tag_summary`` / ``recent_pages``
    / ``pages_touching_topic`` / ``search_pages`` /
    ``find_backlinks``). A regression that drops ``find_backlinks``
    from the instructions (without dropping the tool) surfaces
    here.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"ETag": '"v1"'})

    server = _build(handler)
    text = server.instructions
    assert text is not None
    for journal_tool in (
        "journal_histogram",
        "tag_summary",
        "recent_pages",
        "pages_touching_topic",
        "search_pages",
        "find_backlinks",
    ):
        assert journal_tool in text, (
            f"v1.3 instructions missing journal tool {journal_tool!r}"
        )


@pytest.mark.asyncio
async def test_v1_3_list_tools_returns_fourteen_always_on_tools() -> None:
    """``list_tools`` returns the 14 v1.3 always-on tools by name.

    Mirror of the ``instructions`` test, but against the live
    tool inventory. A regression that removes a v1.3 tool
    from the registration (without dropping it from the
    ``instructions`` text) surfaces here — the agent would
    see a tool promised in the instructions but absent from
    the schema.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"ETag": '"v1"'})

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        tools = await client.list_tools()
        names = {t.name for t in tools.tools}
        expected = {
            "read_page",
            "page_exists",
            "write_page",
            "create_page",
            "delete_page",
            "append_to_page",
            "prepend_to_page",
            "patch_page_lines",
            "patch_page_replace",
            "move_page",
            "list_pages",
            "diff_pages",
            "check_task",
            "list_tasks",
        }
        assert expected.issubset(names), (
            f"v1.3 missing tools: {expected - names}"
        )


# --- T41 doc clarifications -------------------------------------------
#
# T41 lifts three small doc gaps onto the tool surface so an agent
# caller doesn't trip on them in the same session: `read_page`
# returning Space Lua template source (raw markdown, not rendered
# output), `move_page`'s same-name no-op never raising 412 even when
# the caller passes `if_match`, and the `MCPServer.instructions`
# block noting the `.md`-suffix convention lifted by T39. Each is a
# one-sentence addition; the test pins the sentence so a future
# drift surfaces as a test failure rather than as an agent
# confusion in the wild.


@pytest.mark.asyncio
async def test_t41_read_page_description_notes_template_source_vs_render() -> None:
    """T41: ``read_page`` description says template pages are
    raw markdown, never rendered output.

    ``b5`` was the agent reporter's W36 page reading "${template
    .each(...)}" literally and thinking the bridge returned broken
    syntax. The bridge is a transport; it returns whatever SB
    stored. The T41 sentence sits in the description so an agent
    reading the description sees it before the call.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"ETag": '"v1"'})

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        tools = await client.list_tools()
        by_name = {t.name: t for t in tools.tools}
        description = by_name["read_page"].description
    assert "raw markdown source" in description, (
        "T41: read_page description should note raw source vs "
        "rendered output for Space Lua template pages"
    )
    assert "transport, not a renderer" in description, (
        "T41: read_page description should clarify the bridge is "
        "a transport, not a renderer"
    )


@pytest.mark.asyncio
async def test_t41_move_page_description_notes_noop_never_raises_412() -> None:
    """T41: ``move_page`` description says the same-name no-op
    never raises 412 even when ``if_match`` is passed.

    ``b8`` was the agent reporter's expectation that
    ``move_page("Foo", "Foo", if_match=<stale_etag>)`` would
    surface as a 412 if the page had drifted. It doesn't — no
    write happens so no precondition check fires. The T41
    sentence makes the silent no-op contract explicit so an agent
    doesn't wait for a 412 that will never come.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"ETag": '"v1"'})

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        tools = await client.list_tools()
        by_name = {t.name: t for t in tools.tools}
        description = by_name["move_page"].description
    assert "no-op never raises 412" in description, (
        "T41: move_page description should say the same-name "
        "no-op never raises 412"
    )
    assert "if_match=<stale_etag>" in description, (
        "T41: move_page description should reference the "
        "stale-etag + drifted-page scenario by name"
    )


@pytest.mark.asyncio
async def test_t41_instructions_advertise_md_suffix_convention() -> None:
    """T41: ``instructions`` block notes the `.md`-suffix
    convention lifted by T39.

    Now that T39 ships (auto-append `.md` to bare names), the
    bridge's ``MCPServer.instructions`` block carries a single
    sentence so an agent that connects for the first time sees
    the convention in the system-prompt-ish text rather than
    inferring it from the first successful response.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"ETag": '"v1"'})

    server = _build(handler)
    text = server.instructions
    assert text is not None
    assert "automatically suffixed with `.md`" in text, (
        "T41: instructions block should note the T39 .md-suffix "
        "convention"
    )
    assert "Foo.txt" in text, (
        "T41: instructions block should show that names with an "
        "existing extension pass through unchanged (the Foo.txt "
        "example)"
    )


# --- T42: 412 contention hint ---------------------------------------


@pytest.mark.asyncio
async def test_t42_three_412s_no_hint_fourth_carries_concurrent_edit_hint() -> None:
    """T42: after N=3 412s on the same page, the 4th 412
    ``ToolError`` carries ``[concurrent_edit_hint: true]``.

    The first three 412s use the bare ``precondition failed``
    wording (so an agent that pattern-matches on the standard
    wording still matches). The fourth appends the marker so
    an agent that knows the new marker can extract it and back
    off. Threshold matches :data:`_CONTENTION_THRESHOLD` (3).
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(412, text="precondition failed")

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        for i in range(3):
            result = await client.call_tool(
                "write_page",
                {"name": "W36.md", "content": "x", "if_match": "*"},
            )
            assert result.is_error is True
            assert _text(result) == (
                "Error executing tool write_page: precondition "
                "failed; check if_match/if_none_match"
            ), f"412 #{i + 1} should not carry the hint"

        # Fourth 412 trips the threshold.
        result = await client.call_tool(
            "write_page",
            {"name": "W36.md", "content": "x", "if_match": "*"},
        )
    assert result.is_error is True
    assert _text(result) == (
        "Error executing tool write_page: precondition failed; "
        "check if_match/if_none_match [concurrent_edit_hint: true]"
    )


@pytest.mark.asyncio
async def test_t42_counter_is_per_page_not_global() -> None:
    """T42: 1 412 on page A + 1 412 on page B; neither carries
    the hint.

    The contention counter is keyed on ``name`` (one deque per
    distinct page). A single 412 on each page leaves both deques
    at length 1, well below the threshold of 3, so neither 412
    gets the marker. This pins the per-page isolation — a global
    counter (the wrong shape) would have tripped the hint here.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(412, text="precondition failed")

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        for name in ("A.md", "B.md"):
            result = await client.call_tool(
                "write_page",
                {"name": name, "content": "x", "if_match": "*"},
            )
            assert result.is_error is True
            assert _text(result) == (
                f"Error executing tool write_page: precondition "
                f"failed; check if_match/if_none_match"
            ), f"single 412 on {name} should not carry the hint"


@pytest.mark.asyncio
async def test_t42_sliding_window_evicts_old_timestamps(monkeypatch) -> None:
    """T42: after 3 412s within the window, a 60s+ jump clears
    the deque and the next 412 carries no hint.

    Uses ``monkeypatch`` on ``time.monotonic`` to advance the
    clock past :data:`_CONTENTION_WINDOW_SECONDS` without
    sleeping. The deque is bounded to ``_CONTENTION_THRESHOLD``
    entries; advancing the clock past the window evicts all
    three, leaving the deque empty, so the next 412 pushes a
    single entry (length 1, below threshold).
    """
    real_monotonic = time.monotonic
    fake_now = [1000.0]

    def fake_monotonic() -> float:
        return fake_now[0]

    monkeypatch.setattr("mcp_silverbullet.server.time.monotonic", fake_monotonic)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(412, text="precondition failed")

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        # Three 412s on the same page within the window.
        for _ in range(3):
            result = await client.call_tool(
                "write_page",
                {"name": "W36.md", "content": "x", "if_match": "*"},
            )
            assert result.is_error is True
            assert "concurrent_edit_hint" not in _text(result)

        # Jump past the window — every prior timestamp evicts.
        fake_now[0] += 61.0

        result = await client.call_tool(
            "write_page",
            {"name": "W36.md", "content": "x", "if_match": "*"},
        )
    assert result.is_error is True
    assert _text(result) == (
        "Error executing tool write_page: precondition failed; "
        "check if_match/if_none_match"
    )
    assert "concurrent_edit_hint" not in _text(result)


@pytest.mark.asyncio
async def test_t42_successful_write_after_412s_carries_no_hint() -> None:
    """T42: the hint is never raised on the success path.

    Even after three 412s on the same page (which would normally
    trip the hint on the *next* 412), a successful write returns
    the T23 ack envelope without the marker. The hint is purely
    an error-path signal — it never appears on 200 responses.
    This guards against an accidental future change that threads
    the hint into both paths.
    """
    from mcp_silverbullet.server import _CONTENTION_THRESHOLD

    put_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal put_count
        if request.method == "PUT":
            put_count += 1
            if put_count <= _CONTENTION_THRESHOLD:
                return httpx.Response(412, text="precondition failed")
            return httpx.Response(
                200,
                headers={"ETag": '"v1"', "X-Content-Length": "1"},
            )
        # GET — read_page's first call (or the read-modify-write
        # preamble). The success-path branch on the final 412'd
        # write still does a re-read for etag verification.
        return httpx.Response(
            200,
            text="x",
            headers={"ETag": '"v1"', "X-Content-Length": "1"},
        )

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        for _ in range(_CONTENTION_THRESHOLD):
            result = await client.call_tool(
                "write_page",
                {"name": "W36.md", "content": "y", "if_match": "*"},
            )
            assert result.is_error is True
            assert "concurrent_edit_hint" not in _text(result)

        # Fourth call succeeds.
        result = await client.call_tool(
            "write_page",
            {"name": "W36.md", "content": "y", "if_match": "*"},
        )
    assert result.is_error is False
    # The success envelope is the T23 ack; the hint must not
    # surface anywhere on it.
    assert "concurrent_edit_hint" not in _text(result)


# --- T43: CF 5xx cf_hint envelope --------------------------------------


# A representative Cloudflare error envelope — the body the bridge
# sees when a CF-fronted SB 502s on the origin. The shape is taken
# from the user's 2026-08-31 incident (the wrapper's error stream
# surfaced this exact JSON). T43's parser reads only
# ``retry_after``, ``error_code``, and ``title``; the other
# fields are intentionally dropped. The hint rides on the
# error message as a `` [cf_hint: {...}]`` suffix that an agent
# can ``json.loads`` directly to decide whether to retry.
_T43_CF_BODY = (
    '{"type":"https://...","title":"Error 502: Bad gateway",'
    '"status":502,"detail":"...","instance":"a33e...",'
    '"error_code":502,"error_name":"origin_bad_gateway",'
    '"error_category":"origin","ray_id":"a33e...",'
    '"timestamp":"2026-08-31T19:40:15Z","zone":"sb.kesor.net",'
    '"cloudflare_error":true,"retryable":true,"retry_after":60,'
    '"owner_action_required":true,"what_you_should_do":"**Wait '
    'and retry.**...","footer":"..."}'
)


@pytest.mark.asyncio
async def test_t43_cf_5xx_envelope_attaches_cf_hint_to_tool_error() -> None:
    """T43: SB returns 502 with a CF-shaped body; the bridge's
    ``ToolError`` envelope carries `` [cf_hint: {...}]`` carrying
    the parsed ``retry_after`` / ``error_code`` / ``title``.

    Same message-text-channel pattern as T42's
    ``concurrent_edit_hint``: the MCP SDK renders ``ToolError`` as
    plain ``TextContent(text=str(exc))`` — no native envelope
    field exists — so the hint rides as a JSON-serialized suffix
    on the standard wording. An agent that knows the marker can
    ``json.loads`` the suffix and act on ``retry_after`` directly
    without pattern-matching the raw CF JSON body. An agent that
    doesn't know the marker still matches on the standard
    ``silverbullet error: <status>`` wording.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text=_T43_CF_BODY)

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("read_page", {"name": "Foo"})
    assert result.is_error is True
    text = _text(result)
    # Standard wording is unchanged; the marker rides as a suffix.
    assert text.startswith(
        "Error executing tool read_page: silverbullet error: 502"
    )
    # Marker is present and contains the three fields. Parsing the
    # JSON suffix locks the wire shape: an agent can ``json.loads``
    # the part between ``[cf_hint: `` and ``]`` to get the dict.
    assert "[cf_hint: " in text
    start = text.index("[cf_hint: ") + len("[cf_hint: ")
    end = text.rindex("]")
    payload = json.loads(text[start:end])
    assert payload == {
        "retry_after": 60,
        "error_code": 502,
        "title": "Error 502: Bad gateway",
    }


@pytest.mark.asyncio
async def test_t43_non_cf_5xx_envelope_carries_no_cf_hint() -> None:
    """T43: a 5xx with a plain-text body leaves the error
    envelope unchanged (no ``[cf_hint: ...]`` marker).

    Non-CF deployments (SB behind a plain reverse proxy that
    returns its own HTML error page) see no behavior change.
    The pre-T43 wording ``"silverbullet error: <status>"`` is
    surfaced byte-for-byte; the marker is conditional on a
    CF-shaped body, so a non-CF body produces no marker.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="internal server error plain text")

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("read_page", {"name": "Foo"})
    assert result.is_error is True
    assert _text(result) == (
        "Error executing tool read_page: silverbullet error: 500"
    )
    assert "[cf_hint" not in _text(result)


@pytest.mark.asyncio
async def test_t43_5xx_with_non_cf_json_body_carries_no_cf_hint() -> None:
    """T43: a 5xx with random non-CF JSON body leaves the
    envelope unchanged.

    A reverse proxy (nginx with a custom JSON error page, or
    a CF configuration that strips the CF envelope but still
    returns JSON) returns JSON that parses cleanly but carries
    no CF marker fields. The parser detects the absence of
    ``cloudflare_error`` / ``error_category`` / ``ray_id`` and
    returns ``None``, so the tool envelope stays unchanged.
    This pins the conservative posture: only the CF-shaped
    subset of 5xx bodies gets the hint.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text='{"error": "internal", "code": 500}')

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("read_page", {"name": "Foo"})
    assert result.is_error is True
    assert _text(result) == (
        "Error executing tool read_page: silverbullet error: 502"
    )
    assert "[cf_hint" not in _text(result)


@pytest.mark.asyncio
async def test_t43_5xx_with_empty_body_carries_no_cf_hint() -> None:
    """T43: a 5xx with an empty body leaves the envelope unchanged.

    A CF-fronted SB in some failure modes (the
    ``cloudflare_error: true`` set without a JSON body) returns
    a 502 with no bytes. The parser short-circuits on the empty
    body, the tool envelope is the unchanged
    ``"silverbullet error: 502"`` wording, and the agent sees
    the same error string as a non-CF 5xx.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="")

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("read_page", {"name": "Foo"})
    assert result.is_error is True
    assert _text(result) == (
        "Error executing tool read_page: silverbullet error: 502"
    )
    assert "[cf_hint" not in _text(result)


@pytest.mark.asyncio
async def test_t43_cf_body_without_retry_after_surfaces_field_as_none() -> None:
    """T43: a CF-shaped body that omits ``retry_after`` still
    carries the marker, with ``retry_after`` set to ``None``.

    The marker is conditional on the body being CF-shaped
    (``cloudflare_error`` / ``error_category`` / ``ray_id``
    present), not on every field being present. The marker
    schema is *consistent* (``retry_after`` / ``error_code``
    / ``title`` always present, with values that may be
    ``None``) so an agent can read the keys without
    ``KeyError``-guarding. An agent that sees
    ``retry_after: None`` can fall back to a default retry
    interval (e.g. the T42 60-second window) instead of
    pattern-matching on CF's hint structure.
    """
    body = (
        '{"cloudflare_error":true,"error_code":504,'
        '"title":"Error 504: Gateway timeout"}'
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(504, text=body)

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        result = await client.call_tool("read_page", {"name": "Foo"})
    assert result.is_error is True
    text = _text(result)
    assert text.startswith(
        "Error executing tool read_page: silverbullet error: 504"
    )
    assert "[cf_hint: " in text
    start = text.index("[cf_hint: ") + len("[cf_hint: ")
    end = text.rindex("]")
    payload = json.loads(text[start:end])
    assert payload == {
        "retry_after": None,
        "error_code": 504,
        "title": "Error 504: Gateway timeout",
    }


@pytest.mark.asyncio
async def test_t43_successful_write_after_5xx_carries_no_cf_hint() -> None:
    """T43: the ``cf_hint`` marker is never raised on the
    success path.

    Even after the bridge has surfaced the marker on one or
    more 5xx responses, a subsequent successful write returns
    the T23 ack envelope with no marker. The marker is
    purely an error-path signal — it never appears on 200
    responses. This guards against an accidental future
    change that threads the marker into both paths (e.g. a
    per-page counter that lingers on the success envelope).
    """
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            # First read returns 502 with a CF body.
            if not getattr(handler, "_first_read_done", False):
                handler._first_read_done = True  # type: ignore[attr-defined]
                return httpx.Response(502, text=_T43_CF_BODY)
            return httpx.Response(
                200,
                text="# hello",
                headers={
                    "ETag": '"v1"',
                    "X-Last-Modified": "1700000000123",
                    "X-Content-Length": "7",
                },
            )
        return httpx.Response(
            200,
            headers={"ETag": '"v2"', "X-Content-Length": "12"},
        )

    server = _build(handler)
    async with Client(server, raise_exceptions=True) as client:
        # First read 502s with a CF body — the marker is on
        # the error envelope.
        result = await client.call_tool("read_page", {"name": "Foo"})
        assert result.is_error is True
        assert "[cf_hint: " in _text(result)

        # Second read succeeds — no marker on the success path.
        result = await client.call_tool("read_page", {"name": "Foo"})
        assert result.is_error is False
        assert "[cf_hint" not in _text(result)

        # Subsequent write also succeeds — no marker on the
        # success envelope.
        result = await client.call_tool(
            "write_page",
            {"name": "Foo", "content": "# hello"},
        )
        assert result.is_error is False
        assert "[cf_hint" not in _text(result)

"""httpx2 adapter for the SilverBullet `/.fs/...` HTTP API.

The contract is read straight from the upstream SB server (see
``docs/design.md`` § SilverBullet client contract). This module is the
outbound half of the bridge; ``server.py`` calls into it from inside the
MCP tool handlers and translates the exceptions to ``ToolError`` for the
MCP wire.

Four entry points:

- :func:`read_page` — ``GET /.fs/{name}`` — returns
  :class:`PageMeta` with ``body`` + meta (T23 client-side change;
  T24 subsets it down to the read-tool wire shape in
  :func:`_read_meta_to_payload`).
- :func:`write_page` — ``PUT /.fs/{name}`` — returns
  :class:`PageMeta` with the just-written meta (T23).
- :func:`delete_page` — ``DELETE /.fs/{name}`` — returns
  :class:`PageMeta` with the deleted body's ETag and ``None`` for
  size / timestamps (DELETE doesn't echo ``X-*`` per the design
  doc).
- :func:`list_pages` — ``GET /.fs`` — returns
  ``list[PageMeta]`` (T28 widens from the v1 minimal
  ``list[FileMeta]``; same envelope family as read + write).

A fifth entry point, added in v1.2:

- :func:`exists_page` — ``GET /.fs/{name}`` — returns ``bool``
  (T25): ``True`` for 200, ``False`` for 404, raises
  :class:`ServerError` on 5xx so the caller can distinguish
  "doesn't exist" from "SB is broken". Body bytes are never
  materialized; this is a cheap existence check, not a read.

A sixth entry point, also v1.2 T28:

- :func:`read_page_meta` — ``GET /.fs/{name}`` via
  ``httpx.AsyncClient.stream`` — returns :class:`PageMeta`
  *without* materializing the body. The list-pages etag-hydration
  walker uses this to fetch per-page etags when the operator opts
  in via
  ``MCP_SILVERBULLET_LIST_PAGES_HYDRATE_ETAGS``; the SB list
  payload omits the etag field on this build, so a per-page GET
  is the only way to surface one. The "safe" sibling
  :func:`read_page_meta_safe` swallows transient failures so a
  single page 404'ing / 412'ing / timing out doesn't abort the
  whole list_pages call.

The status-code mapping lives in :func:`_raise_for_status` and matches
the table in ``docs/design.md`` § Tools. The PUT envelope carries the
full header set the design doc § SilverBullet client contract calls
out for writes (``X-Source: external``, ``X-Permission: rw``,
``X-Created``, ``X-Last-Modified``, ``X-Content-Length``,
``Content-Type: text/markdown``, optional ``If-Match`` /
``If-None-Match``) so SB's attribution log distinguishes bridge
writes from editor / sync writes, and so future SB versions that
honor request-side meta can read it without a bridge change.

``FileMeta`` (the v1 minimal ``name`` / ``etag`` subset) is kept
on the module surface for back-compat — T28 widened
:func:`list_pages` to return :class:`PageMeta`, but the
dataclass itself is still a valid projection of the full envelope
and a future caller may want the narrower shape without paying
for the four extra fields.

The GET and PUT response ``X-*`` headers (``X-Created``,
``X-Last-Modified``, ``X-Content-Length``) plus ``ETag`` flow into
:class:`PageMeta` via :func:`_meta_from_response`. Any missing /
malformed header becomes ``None`` (defensive parse via
:func:`_parse_int_header`) — same shape as the prior ``None`` ETag
handling on older SB / proxy-stripped responses.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

import httpx2 as httpx


# Body limit printed verbatim into the error; matches the SDK's
# ``max_request_body_size`` default (4 MiB).
_BODY_LIMIT_BYTES = 4 * 1024 * 1024

# Per-call fields (``X-Created`` / ``X-Last-Modified`` /
# ``X-Content-Length``) are stamped at request time so each write
# reflects the moment the PUT leaves the bridge; the static fields
# below are inherited by every call.
_WRITE_HEADERS = {
    # Required by SilverBullet's `VALID_WRITE_SOURCES` so the attribution
    # log tags the write as coming from the bridge, not the editor.
    "X-Source": "external",
    # Bridge has read+write on the space; SB rejects writes without
    # `X-Permission: rw`.
    "X-Permission": "rw",
}

# DELETE only needs ``X-Source`` — the design doc § SilverBullet
# client contract DELETE row lists ``X-Source: external`` and an
# optional ``If-Match`` and nothing else. We deliberately do NOT
# reuse ``_WRITE_HEADERS`` here: that constant adds ``X-Permission:
# rw``, which PUTs need and DELETEs are not documented to require
# (and a future SB that tightens DELETE-only behavior — e.g.,
# gating on ``X-Permission: rdonly`` — won't be confused by an
# unsolicited header).
_DELETE_HEADERS = {
    "X-Source": "external",
}


def _epoch_ms() -> int:
    """Epoch milliseconds, the unit SB's ``header_i64`` parses.

    Matches the ``FileMeta.created`` / ``FileMeta.last_modified`` shape
    in ``silverbullet_server_common::types::FileMeta`` (epoch ms). The
    disk impl of ``write_file`` honors ``last_modified > 0`` by
    stamping the file's mtime, but ignores ``created`` / ``size`` /
    ``perm`` — so we send them all (per the design doc § SilverBullet
    client contract PUT row) and let SB pick what it wants.
    """
    return time.time_ns() // 1_000_000


class SBError(Exception):
    """Base class for SB client errors. Never raised directly."""


class PageNotFound(SBError):
    """SB returned 404 for a read or write target."""


class PreconditionFailed(SBError):
    """SB returned 412 — ``If-Match`` / ``If-None-Match`` not satisfied."""


class BodyTooLarge(SBError):
    """SB returned 413 — body exceeds the 4 MiB SDK limit."""


class ServerError(SBError):
    """SB returned a 5xx status. Carries the status code.

    T43: when the 5xx response body looks CF-shaped (a Cloudflare
    error page wrapping an ``origin_bad_gateway`` /
    ``origin_unreachable`` / etc. failure), the optional
    ``cf_hint`` attribute carries a structured dict of the bits
    an agent needs to decide whether to retry: ``retry_after``
    (seconds, may be ``None`` if the upstream didn't include it),
    ``error_code`` (the numeric CF error code, e.g. ``502``), and
    ``title`` (the human-readable summary, e.g. ``"Error 502:
    Bad gateway"``). Other CF fields (``ray_id``, ``zone``,
    ``instance``, etc.) are deliberately dropped — they're useful
    for debugging the CF/proxy layer, not for the agent's
    decision-making. ``cf_hint`` is ``None`` for any 5xx whose
    body doesn't look CF-shaped (the common case: SB behind a
    plain reverse proxy that returns a non-JSON HTML error page,
    or an empty body). The MCP tool layer reads ``cf_hint`` and
    attaches it to the error envelope so an MCP-SDK-aware wrapper
    can surface the hint cleanly; a wrapper that unwraps to raw
    body still gets the same CF JSON, but the bridge has done its
    part by giving the structured envelope.
    """

    cf_hint: dict[str, Any] | None = None


@dataclass(frozen=True)
class FileMeta:
    """Minimal subset of SB's ``FileMeta`` we surfaced pre-T28.

    SB returns more fields (``createdAt``, ``lastModified``, ``size``,
    ``contentType``); v1 only needed ``name`` for ``list_pages`` and
    ``etag`` for the optional ``If-Match`` round-trip. v1.2's T28
    widened ``list_pages`` to return :class:`PageMeta` (the same
    envelope family the read/write tools use); ``FileMeta`` itself
    stays on the module surface for back-compat — a caller that
    wants the narrower shape filters the wider list client-side
    (``[FileMeta(name=r.name, etag=r.etag) for r in
    sb.list_pages()]``) rather than going through a separate
    client method.
    """

    name: str
    etag: str | None = None


@dataclass(frozen=True)
class PageMeta:
    """Single-source-of-truth acknowledgement shape for read + write tools.

    v1.2 T23 (write tools), T24 (read tools), and T28 (``list_pages``
    rows) all return this shape so an agent that just made a write
    knows ``size_bytes`` / ``last_modified_ms`` / ``created_ms``
    without a follow-up read, a read returns the same envelope so a
    caller doesn't have to learn two shapes, and a list call
    returns ``list[PageMeta]`` so the same envelope powers all
    three surfaces — one dataclass, every tool.

    The MCP tool layer subsets this shape per ticket: T23's write
    tools emit ``{name, etag, size_bytes, last_modified_ms, created_ms}``
    (no body — writes return meta); T24's read tool emits
    ``{body, etag, size_bytes, last_modified_ms}`` (no name, no
    ``created_ms`` — the caller already knows the name, and read
    has no create-vs-update distinction to surface). The underlying
    client carries the full envelope; the tool chooses which fields
    to forward.

    Every field except ``name`` is optional because SB's response
    headers are best-effort (older builds, proxy-stripped, some
    operators behind a CDN that drops ``X-*``). The design doc §
    SilverBullet client contract documents these headers on the GET
    / PUT rows but not on DELETE, so a ``delete_page`` round trip
    will typically carry ``size_bytes = None`` (DELETE doesn't echo
    the body length), ``last_modified_ms = None`` /
    ``created_ms = None`` (DELETE doesn't echo the timestamps), and
    ``body = None`` (DELETE has no body to surface). The honest wire
    shape is "all None where SB doesn't carry the field", not
    fabricating numbers; an agent that wants the timestamps of what
    it deleted reads the page first (``read_page`` will surface them).

    ``body`` is the only field the write paths populate ``None`` for;
    it's set by ``read_page`` to the markdown text and ``None`` on
    every write/delete return. Carried on the same envelope so the
    client method has one return type.

    Field names mirror the design doc header names so a future
    bridge-side maintainer doesn't have to map ``X-Last-Modified`` to
    ``modified_at`` or similar: ``X-Last-Modified`` →
    ``last_modified_ms``, ``X-Created`` → ``created_ms``,
    ``X-Content-Length`` → ``size_bytes`` (bytes, not codepoints,
    matching SB's ``meta.size``).
    """

    name: str
    etag: str | None = None
    size_bytes: int | None = None
    last_modified_ms: int | None = None
    created_ms: int | None = None
    body: str | None = None


class SBClient:
    """Async client for SilverBullet's ``/.fs/...`` endpoints.

    Parameters
    ----------
    base_url
        Origin of the SB server, e.g. ``http://127.0.0.1:63000``. No
        trailing slash.
    token
        Shared bearer secret; same value as ``MCP_SILVERBULLET_TOKEN``
        and ``SB_AUTH_TOKEN``.
    timeout
        Read / write timeouts in seconds. ``None`` (default) selects
        a 10 s read / write timeout with a 3 s connect timeout — long
        enough for a sleepy SB that's also serving the SPA shell over
        the same loopback, short enough that a hung SB surfaces as a
        tool error rather than a stuck agent turn. Operators that
        need different values pass an :class:`httpx.Timeout` directly.
    """

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout: httpx.Timeout | None = None,
    ) -> None:
        headers: dict[str, str] = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers=headers,
            timeout=timeout or httpx.Timeout(10.0, connect=3.0),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "SBClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    # --- endpoints -----------------------------------------------------

    async def read_page(self, name: str) -> PageMeta:
        """Return the markdown body + metadata for ``/.fs/{name}``.

        The body's text lives in ``result.body``; the ``X-*`` meta
        headers from SB's response live alongside the body in the
        same :class:`PageMeta` (matching the T24 read-tool wire
        shape — a dict with ``body`` plus the meta fields). v1.1
        returned ``str``; the T23 client-side change widened this
        client method to return :class:`PageMeta` so T24's MCP-tool
        widening (subset the envelope via
        :func:`_read_meta_to_payload` in :mod:`server`) was a
        one-line change rather than a round trip through the
        client.

        Raises :class:`PageNotFound` if the page is missing.
        """
        response = await self._client.get(f"/.fs/{name}")
        _raise_for_status(response)
        return _meta_from_response(response, name, body=response.text)

    async def exists_page(self, name: str) -> bool:
        """Cheap existence check: ``GET /.fs/{name}`` → ``True`` / ``False``.

        Translates ``200`` → ``True``, ``404`` → ``False``, ``5xx``
        → :class:`ServerError`, ``412`` → :class:`PreconditionFailed`,
        ``413`` → :class:`BodyTooLarge` (each via the standard
        :func:`_raise_for_status` mapping — the SB client's typed
        exceptions are the canonical surface for "this went wrong",
        and the MCP-tool handler translates them to ``ToolError``
        per the design doc). The body bytes are never materialized
        (we don't read ``response.text`` / ``response.content``) —
        the call is one round-trip with the headers only, which is
        cheaper than a full :func:`read_page` for the "does this
        page exist?" question. If the caller also wants the etag
        / size / body, :func:`read_page` is one round trip away;
        this method is for the *existence-only* case.

        Why a GET and not HEAD: SB's ``/.fs`` endpoint is documented
        to honor ``GET`` but ``HEAD`` semantics aren't part of the
        upstream contract we lock against (``server/src/handlers/
        fs.rs``), so a HEAD could behave differently across SB
        versions. GET is the wire-level primitive the design doc
        guarantees.

        Why ``ServerError`` on 5xx rather than returning ``False``:
        the caller asked a definitive question ("does the page
        exist?") and "I don't know, the server is broken" is not a
        valid ``False`` answer. The MCP-tool handler surfaces the
        exception as a :exc:`ToolError` so the agent sees the
        difference between a real "no" (proceed with create /
        skip) and "SB is down, don't make decisions" (retry or
        surface to the user).

        A 412 / 413 on a GET is unexpected — preconditions and
        body-size limits live on writes, not reads — but either
        would indicate a proxy / SB misconfiguration, so we let it
        propagate via the standard :func:`_raise_for_status` mapping
        for the tool layer to surface the same way it does for the
        read tools. Without this, an unhandled ``PreconditionFailed``
        or ``BodyTooLarge`` would leak as a generic ``MCPError``
        rather than the design-doc ``ToolError`` wording.

        The function does not wrap in :func:`_translate_sb_errors`
        (that's an MCP-layer concern); instead, it surfaces
        ``ServerError`` / ``PreconditionFailed`` / ``BodyTooLarge``
        directly and lets the MCP tool handler translate to
        ``ToolError``. Timeouts bubble up as
        :class:`httpx.TimeoutException` (the SB client's standard
        exception type); the tool layer translates those too.
        """
        response = await self._client.get(f"/.fs/{name}")
        if response.status_code == 200:
            return True
        if response.status_code == 404:
            return False
        # Every other status (5xx, unexpected 4xx, future 2xx that
        # isn't 200) gets handed to ``_raise_for_status`` so the
        # exception type and wording stay in one place. ``False`` is
        # the *only* "no" answer; an unexpected status (e.g. a 204
        # if SB ever returns one for an empty page, or a 500)
        # surfaces as a typed exception so the caller gets a clear
        # error rather than a quietly wrong yes/no.
        _raise_for_status(response)
        # ``_raise_for_status`` returns silently on 2xx and raises
        # on everything else, so the only reachable path here is
        # the future-proofing case: a 2xx other than 200 should
        # count as "exists" (the safest answer to the existence
        # question if SB ever adds, e.g., a 204 No Content for an
        # empty page). Today no such response is documented; this
        # line is defensive rather than load-bearing.
        return True

    async def write_page(
        self,
        name: str,
        content: str,
        *,
        if_match: str | None = None,
        if_none_match: bool = False,
    ) -> PageMeta:
        """Create or update ``/.fs/{name}``.

        Parameters
        ----------
        content
            Raw UTF-8 markdown body.
        if_match
            Optional ``If-Match`` value (``"*"`` to require existence,
            or an ETag for an exact-body match).
        if_none_match
            If true, send ``If-None-Match: *`` so SB refuses if the
            page already exists. Mutually exclusive with ``if_match``;
            ``if_match`` wins if both are set.

        Returns
        -------
        PageMeta
            Acknowledgement envelope (T23): ``{name, etag,
            size_bytes, last_modified_ms, created_ms}``. The
            ``size_bytes`` is the UTF-8 byte count of the just-written
            body (``len(content.encode("utf-8"))``) — independent of
            whether SB echoes ``X-Content-Length`` back, so it's never
            ``None`` for a successful write even when the proxy strips
            ``X-*`` response headers. ``last_modified_ms`` and
            ``created_ms`` come from ``X-Last-Modified`` /
            ``X-Created`` and are ``None`` if the response didn't
            carry them (older SB / proxy-stripped — same contract drift
            note as the prior None-ETag handling).

        Raises
        ------
        PageNotFound, PreconditionFailed, BodyTooLarge, ServerError
        """
        headers = dict(_WRITE_HEADERS)
        # SB's `/.fs` PUT handler reads ``Content-Type`` to decide how to
        # store the body; httpx doesn't auto-set it for ``content=str``.
        headers["Content-Type"] = "text/markdown"
        # Full design-doc envelope (T8): ``X-Created``, ``X-Last-Modified``,
        # ``X-Content-Length`` round out the PUT request headers per
        # ``docs/design.md`` § SilverBullet client contract. Values are
        # stamped at request time so each write reflects the moment
        # the PUT leaves the bridge (``X-Content-Length`` is the UTF-8
        # byte count, matching SB's ``meta.size``).
        now_ms = _epoch_ms()
        headers["X-Created"] = str(now_ms)
        headers["X-Last-Modified"] = str(now_ms)
        headers["X-Content-Length"] = str(len(content.encode("utf-8")))
        if if_match is not None:
            headers["If-Match"] = if_match
        elif if_none_match:
            headers["If-None-Match"] = "*"

        response = await self._client.put(
            f"/.fs/{name}",
            content=content,
            headers=headers,
        )
        _raise_for_status(response)
        # T23: surface ``size_bytes`` from the *request* body (UTF-8
        # bytes), not from the response. The request-side
        # ``X-Content-Length`` is what we sent (matches SB's
        # ``meta.size``); the response may or may not echo it back.
        # Reporting the byte count we wrote is the honest number — an
        # agent that asks "how big is the page now?" gets the size of
        # what it just wrote, which matches SB's view even when the
        # response header is stripped.
        meta = _meta_from_response(response, name)
        return PageMeta(
            name=name,
            etag=meta.etag,
            size_bytes=len(content.encode("utf-8")),
            last_modified_ms=meta.last_modified_ms,
            created_ms=meta.created_ms,
        )

    async def delete_page(
        self,
        name: str,
        *,
        if_match: str | None = None,
    ) -> PageMeta:
        """Delete ``/.fs/{name}``.

        Parameters
        ----------
        name
            Page name (the SB path segment after ``/.fs/``).
        if_match
            Optional ``If-Match`` value (``"*"`` to require existence,
            or an ETag for an exact-body match). ``None`` (default)
            means unconditional delete.

        Returns
        -------
        PageMeta
            Acknowledgement envelope (T23): ``{name, etag,
            size_bytes=None, last_modified_ms=None, created_ms=None,
            body=None}``. SB's DELETE response only carries the
            deleted body's ETag (per the design doc § SilverBullet
            client contract DELETE row); the rest are ``None`` rather
            than fabricated. An agent that wants the timestamps of
            what it about to delete can ``read_page`` first to
            capture them, then ``delete_page`` with ``if_match``.

        Raises
        ------
        PageNotFound, PreconditionFailed, ServerError
        """
        headers = dict(_DELETE_HEADERS)
        if if_match is not None:
            headers["If-Match"] = if_match
        response = await self._client.delete(
            f"/.fs/{name}",
            headers=headers,
        )
        _raise_for_status(response)
        # DELETE doesn't echo ``X-*`` meta per the design doc §
        # SilverBullet client contract DELETE row — only ``ETag`` (the
        # deleted body's hash) is carryable. The bridge surfaces
        # ``None`` for ``size_bytes`` / ``last_modified_ms`` /
        # ``created_ms`` rather than fabricating them; an agent that
        # wants the timestamps of what it deleted reads the page
        # first (``read_page`` will surface them).
        return _meta_from_response(response, name)

    async def list_pages(self) -> list[PageMeta]:
        """Return :class:`PageMeta` for every page in the space.

        SB returns a JSON array of file-meta objects (**only** when
        the request carries ``X-Sync-Mode`` — without it, SB
        307-redirects ``GET /.fs`` to the SPA UI). The list payload
        per ``server/src/handlers/fs.rs::handle_fs_list`` carries
        ``name`` / ``created`` / ``lastModified`` / ``contentType`` /
        ``size`` / ``perm``; on this SB build it does **not** carry an
        ``etag`` field (the v1 map's T10 decision documented this), so
        ``etag`` is ``None`` for every row until SB starts emitting
        one. The bridge threads ``name`` / ``created`` /
        ``lastModified`` / ``size`` / ``etag`` into the
        :class:`PageMeta` shape; ``contentType`` and ``perm`` are
        dropped (no caller has asked for them; surfacing them would
        grow the wire shape without a use case). T28 widens this
        from the v1.1 ``list[FileMeta]`` (the minimal ``name`` /
        ``etag`` subset) to ``list[PageMeta]`` (the same envelope
        family the read and write tools use), so a single tool now
        returns everything the agent would otherwise have to
        ``read_page`` for. The MCP tool layer subsets ``PageMeta``
        down to the T23 wire shape (no ``body``) per row.

        See :meth:`read_page_meta` for the per-page etag-hydration
        fallback (opt-in via the bridge's
        ``MCP_SILVERBULLET_LIST_PAGES_HYDRATE_ETAGS`` env var) —
        the list payload's missing ``etag`` field is a per-build
        gap, not a permanent one, and an operator who needs an
        ``if_match`` round-trip can pay the N+1 cost of a per-page
        GET to hydrate it.
        """
        response = await self._client.get(
            "/.fs", headers={"X-Sync-Mode": "1"}
        )
        _raise_for_status(response)
        data = response.json()
        if not isinstance(data, list):
            # SB contract is "array of FileMeta"; anything else is a
            # server-side bug we'd want to know about loudly.
            raise ServerError(f"unexpected /.fs response: {type(data).__name__}")
        return [
            _page_meta_from_list_item(item)
            for item in data
            if isinstance(item, dict) and "name" in item
        ]

    async def read_page_meta(self, name: str) -> PageMeta:
        """Fetch a page's metadata *without* materializing the body.

        Issues ``GET /.fs/{name}`` and reads only the response
        headers (``ETag`` / ``X-Created`` / ``X-Last-Modified`` /
        ``X-Content-Length``) — the body is closed before it's
        buffered, so the round trip costs the network headers but
        not the body bytes. This is the per-page hydration path the
        ``list_pages`` tool uses when the operator opts in to
        ``MCP_SILVERBULLET_LIST_PAGES_HYDRATE_ETAGS``: SB's list
        payload omits ``etag`` on this build, so the bridge falls
        back to one GET per page to surface the etag an agent
        would otherwise need a full :func:`read_page` for.

        The same status-code mapping as :func:`read_page` applies:
        404 → :class:`PageNotFound`, 5xx → :class:`ServerError`,
        etc. A 200 with no ``X-*`` headers surfaces as the same
        ``None``-populated envelope a fully-stripped response
        would — the call is metadata-first; the body is never
        seen, let alone parsed.

        ``httpx.AsyncClient.stream`` is the right primitive here
        over a plain ``get``: it returns an :class:`httpx.Response`
        with the headers already populated (so we can read
        ``ETag`` / ``X-*`` immediately), and we can ``aclose()`` it
        before httpx buffers the body. A plain ``get`` would buffer
        the body in the background; closing mid-read still works
        (httpx drops the connection) but the explicit stream +
        ``aclose`` makes the intent obvious to the next reader and
        avoids any background-task leak on a slow SB.
        """
        async with self._client.stream("GET", f"/.fs/{name}") as response:
            # ``response.headers`` is populated as soon as the
            # response-line + headers are received; we don't read
            # ``response.text`` / ``response.content`` so the body
            # is never buffered. The ``__aexit__`` on
            # ``httpx.Response`` (the async stream manager) closes
            # the connection cleanly on the way out, dropping the
            # unconsumed body — the body's been on the wire but
            # httpx never copies it into a Python string.
            _raise_for_status(response)
            return _meta_from_response(response, name, body=None)

    async def read_page_meta_safe(self, name: str) -> PageMeta | None:
        """Like :meth:`read_page_meta`, but swallows transient failures.

        Used by the list-pages etag-hydration walker: a single page
        404'ing (deleted between list and hydrate), 412'ing (a
        proxy / SB misconfiguration), or timing out shouldn't
        abort the whole ``list_pages`` call. On any of those
        failures the helper returns ``None`` so the caller can
        keep the row's ``etag=None`` from the list payload rather
        than surfacing the failure to the agent. A 200 returns the
        full :class:`PageMeta` for the caller to merge.
        """
        try:
            return await self.read_page_meta(name)
        except (PageNotFound, PreconditionFailed, ServerError, httpx.TimeoutException):
            # 5xx / 412 / timeout / 404 — any of these is
            # page-local; the surrounding list_pages call should
            # still return a complete list (with this row's
            # etag left as None).
            return None


def _meta_from_response(
    response: httpx.Response, name: str, *, body: str | None = None
) -> PageMeta:
    """Extract the PageMeta envelope from SB's response headers.

    Every ``PageMeta`` field is optional: SB may strip ``X-*`` headers
    (older builds, proxy / CDN), and DELETE doesn't carry them per
    the design doc. ``body`` is the markdown text (only meaningful
    for ``read_page``; writes/deletes pass ``None`` so the returned
    PageMeta is a strict meta envelope without a stray body).

    The integer headers (``X-Created`` / ``X-Last-Modified`` /
    ``X-Content-Length``) are defensively wrapped in try/except —
    SB sends epoch-millisecond integers, but a misconfigured proxy
    could substitute a non-numeric string and we don't want that to
    crash the whole write. Any parse failure becomes ``None``,
    matching the prior "older / proxy-stripped" stance.
    """
    return PageMeta(
        name=name,
        etag=_etag_from_response(response),
        size_bytes=_parse_int_header(response.headers.get("X-Content-Length")),
        last_modified_ms=_parse_int_header(response.headers.get("X-Last-Modified")),
        created_ms=_parse_int_header(response.headers.get("X-Created")),
        body=body,
    )


def _etag_from_response(response: httpx.Response) -> str | None:
    """Read the ``ETag`` response header, falling back to a synthesized value.

    T31's live verification surfaced the v1.3 concurrency-blocking
    fact: SB on this dev box returns no ``ETag`` header on
    ``PUT /.fs/{name}``. Without a fallback the read-side envelope
    surfaces ``etag=None`` on every PUT, and any caller that
    threads ``read().etag`` into ``write(if_match=<read_etag>)``
    threads ``None`` — no precondition at all. The fix (T31a):
    synthesize a stable etag from the headers SB *does* return.

    The synthesized form is ``"{size_bytes}"`` (T44) — a single
    quoted integer string derived from ``X-Content-Length``.
    Stability: two reads of the same body produce the same
    synthesized etag (same byte count); two reads of different
    bodies produce different synthesized etags. That's the entire
    concurrency primitive the agent needs — detect that *what*
    changed between read and write, not *when*. T44 dropped the
    prior ``"{last_modified_ms}-{size_bytes}"`` shape because the
    bridge stamps ``X-Last-Modified`` with ``now_ms`` on every
    PUT request (``_WRITE_HEADERS``), so the post-write re-read's
    mtime drifts from the pre-write read's mtime even when no
    concurrent edit happened — the mtime component was tracking
    *when* a write happened, not *what* it wrote, and the *when*
    drift was the source of T31b's false-positive "concurrent
    edit detected" on every successful write.

    Fallback chain when ``ETag`` is missing:

    - ``X-Content-Length`` present → ``"{bytes}"`` (the normal
      case on this SB build; both headers are populated by SB
      on every PUT response per T31's resolution).
    - ``X-Content-Length`` missing / malformed → ``None`` (no
      fallback available; the agent loses the concurrency
      primitive, same as on a fully-stripped response pre-T31a).
      Pre-T44 the helper would have surfaced ``"{ms}"`` from the
      timestamp header, but a timestamp-only value is *less*
      useful than a size-only value (it can't distinguish two
      writes in the same epoch-ms window with different bodies
      *and* it's what was tracking the *when* drift that caused
      the T31b false-positive). Returning ``None`` here is the
      honest answer: the bridge has no useful primitive to offer.

    The fallback never round-trips to SB — it's a local
    derivation from headers already on the response. The agent
    sees the same envelope shape regardless of whether the etag
    is real or synthesized (no separate flag surfaced; a future
    caller that wants the distinction can compare the etag
    against an explicit ``"synthetic:"`` prefix if it ever
    matters, but no ticket in v1.3 / v1.5 asks for that).

    Wire-shape change (T44): the synthesized form went from
    ``"{ms}-{bytes}"`` to ``"{bytes}"``. Callers that persist
    a v1.3 / v1.4 synthesized etag across calls will see a
    mismatch after the T44 fix lands and should re-read once to
    pick up the new canonical form. Real ETag headers (from SB
    builds that emit them) are unaffected.
    """
    raw = response.headers.get("ETag")
    if raw:
        return raw
    last_modified_ms = _parse_int_header(
        response.headers.get("X-Last-Modified")
    )
    size_bytes = _parse_int_header(
        response.headers.get("X-Content-Length")
    )
    return synthesize_etag(last_modified_ms, size_bytes)


def synthesize_etag(
    last_modified_ms: int | None, size_bytes: int | None
) -> str | None:
    """Build the synthetic-etag fallback string from a PUT response's
    ``X-Content-Length`` (and ``X-Last-Modified``, retained for
    call-site compatibility but unused).

    Exposed at module scope so :mod:`server` can construct the same
    value when comparing two reads against each other (the T31b
    post-write verification path compares ``read_post.etag`` against
    ``expected_etag``; if both are synthesized, the comparison uses
    the same function on both sides and they match by construction
    when the body hasn't drifted).

    The wire shape is ``"{size_bytes}"`` when ``X-Content-Length``
    is present and ``None`` otherwise. The quotes around the
    synthesized value mirror SB's own ETag header (``"<...>"``);
    they're not load-bearing for SB-on-this-build (which ignores
    ``If-Match`` outright per T31's resolution) but they keep
    the value shape indistinguishable from a real ETag, which
    matters on any future SB build that *does* honor the header.

    Why size alone is the right primitive (T44): the concurrency
    primitive needs to differ between two *different* bodies —
    a body-length-derived value satisfies that (same body → same
    size → same etag; different body → different size → different
    etag). The pre-T44 ``"{ms}-{bytes}"`` shape tracked *when* a
    write happened on top of *what* it wrote; the *when*
    component drifted on every PUT (the bridge stamps
    ``X-Last-Modified`` with ``now_ms`` per ``_WRITE_HEADERS``),
    so two reads of the same body produced different synthesized
    etags and T31b's verification helper raised "concurrent
    edit detected" on every successful write. The mtime was
    the wrong axis for the concurrency primitive: it changes
    on every write regardless of whether the body changed.

    ``size_bytes`` is the only required argument. The
    ``last_modified_ms`` parameter is kept (so call sites don't
    need to change) but is unused — pre-T44 it was load-bearing
    in the dashed form; T44 drops it.
    """
    if size_bytes is None:
        return None
    return f'"{size_bytes}"'


def _page_meta_from_list_item(item: dict[str, object]) -> PageMeta:
    """Build a :class:`PageMeta` from one row of SB's ``GET /.fs`` list payload.

    The list payload's keys per ``server/src/handlers/fs.rs`` are
    ``name`` / ``created`` / ``lastModified`` / ``contentType`` /
    ``size`` / ``perm`` (and ``etag`` on SB builds that emit one).
    We map them onto :class:`PageMeta` field-for-field:

    - ``name`` → ``name``
    - ``created`` → ``created_ms`` (epoch ms, per SB's
      ``FileMeta.created`` shape)
    - ``lastModified`` → ``last_modified_ms`` (epoch ms)
    - ``size`` → ``size_bytes`` (UTF-8 byte count, matches
      SB's ``FileMeta.size``)
    - ``etag`` → ``etag`` (string with surrounding quotes, same
      shape as the GET / PUT response headers — ``None`` on this
      SB build, which is what triggers the etag-hydration fallback
      in :meth:`SBClient.read_page_meta_safe`)

    ``contentType`` and ``perm`` are intentionally dropped: no
    caller has asked for them, surfacing them would grow the wire
    shape without a use case, and they're already documented at
    the SB level (operators who need them can read individual
    pages or query SB directly).

    Every field is defensive-parsed via :func:`_parse_int_header`
    for integers and ``or None`` for strings — a misconfigured
    proxy, an SB-side schema drift, or a future SB that emits a
    non-numeric ``created`` should surface as ``None`` rather than
    a crash that takes the whole list call down. The "honest
    None" shape matches the read / write paths: an agent that
    gets a ``list_pages` row with ``created_ms=None`` knows SB
    didn't carry the field for this row, same as a read with
    ``X-Created`` stripped.
    """
    name = item.get("name")
    etag = item.get("etag")
    return PageMeta(
        name=name if isinstance(name, str) else "",
        etag=etag if isinstance(etag, str) else None,
        size_bytes=_parse_int_header(_coerce_str(item.get("size"))),
        last_modified_ms=_parse_int_header(_coerce_str(item.get("lastModified"))),
        created_ms=_parse_int_header(_coerce_str(item.get("created"))),
        body=None,
    )


def _coerce_str(value: object) -> str | None:
    """Coerce a JSON-decoded value to ``str`` for :func:`_parse_int_header`.

    ``json.loads`` decodes JSON numbers as Python ``int`` /
    ``float`` and JSON strings as ``str``. SB sends epoch-ms
    integers for ``created`` / ``lastModified`` / ``size`` so
    :func:`_parse_int_header` expects ``str``; this helper
    bridges the type without making the caller think about it
    (``int`` becomes the decimal string, ``None`` stays
    ``None``, anything else is coerced via ``str(value)`` which
    is good enough for the malformed-value case — the int parse
    downstream will reject the result and surface ``None``).
    """
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        # ``bool`` is a subclass of ``int``; ``str(True)`` is
        # ``"True"`` which the int parse rejects → ``None``.
        # Defensive against a future SB that emits a JSON bool for
        # one of these fields.
        return str(value)
    if isinstance(value, int):
        return str(value)
    return str(value)


def _parse_int_header(value: str | None) -> int | None:
    """Parse an integer response header; ``None`` on missing or malformed.

    Defensive wrapper for SB's epoch-ms headers (``X-Created`` /
    ``X-Last-Modified``) and ``X-Content-Length``. A non-numeric value
    (proxy substitution, encoding drift) becomes ``None`` — same
    shape as a missing header — so an agent always sees ``int | None``
    and never has to handle ``ValueError``.
    """
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# T43: marker fields that identify a Cloudflare-shaped 5xx body. CF's
# error pages wrap every 5xx in a JSON envelope carrying a few
# diagnostic fields (``ray_id`` for tracing, ``error_category`` for
# classification, ``cloudflare_error`` as a self-identifier). When any
# of these is present we know the body is a CF wrapper rather than SB's
# own error format (or a plain reverse-proxy HTML page); we then extract
# the three fields useful for an agent's retry decision. The marker
# check is deliberately permissive — *any* of the three fields triggers
# the parser, not all three — because CF's body shape has shifted
# across releases and we don't want a future CF format change to
# silently drop the hint.
_CF_MARKER_FIELDS = ("cloudflare_error", "error_category", "ray_id")


def _parse_cf_error(body: str | None) -> dict[str, Any] | None:
    """Return a structured hint for a CF-shaped 5xx body, else ``None``.

    T43: parses Cloudflare's JSON error envelope (the body that
    surfaces when a CF-fronted SB 502s / 503s / 504s on the origin)
    and extracts the three fields an agent needs to decide
    whether to retry:

    - ``retry_after`` (seconds, may be ``None`` if upstream omitted it)
    - ``error_code`` (the numeric CF error code, e.g. ``502``)
    - ``title`` (the human-readable summary, e.g. ``"Error 502:
      Bad gateway"``)

    Returns ``None`` when the body doesn't look CF-shaped: empty
    body, body that isn't valid JSON, or body that's JSON but
    carries none of the CF marker fields. The marker check is
    deliberately permissive — *any* of
    ``"cloudflare_error"``, ``"error_category"``, or ``"ray_id"``
    triggers the parse, so a future CF format change that drops one
    field doesn't silently disable the hint.

    Other CF fields (``ray_id``, ``zone``, ``instance``,
    ``error_name``, ``timestamp``, ``what_you_should_do``, etc.)
    are intentionally dropped — they're useful for debugging the
    CF/proxy layer, not for the agent's decision-making. Surfacing
    them as part of the hint would add noise without a useful
    signal.

    Defensive against malformed input: ``json.JSONDecodeError``
    (body isn't valid JSON) and ``TypeError`` (body is bytes /
    ``None`` / a non-string) both return ``None`` rather than
    raising. The caller is a 5xx error path already; propagating
    a parser exception would mask the original 5xx with a
    secondary failure.
    """
    if not body:
        return None
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    if not any(field in data for field in _CF_MARKER_FIELDS):
        return None
    # Surface the three useful fields. ``retry_after`` is allowed to
    # be missing from the upstream payload (not every CF 5xx carries
    # a retry hint); the field is *always* present in the returned
    # dict when the body is CF-shaped, but its value can be ``None``.
    # ``error_code`` may be numeric (502) or string-typed depending on
    # CF release; coerce to ``int`` for caller convenience.
    error_code = data.get("error_code")
    if isinstance(error_code, str):
        try:
            error_code = int(error_code)
        except (TypeError, ValueError):
            pass
    return {
        "retry_after": data.get("retry_after"),
        "error_code": error_code,
        "title": data.get("title"),
    }


def _raise_for_status(response: httpx.Response) -> None:
    """Translate SB HTTP statuses to typed exceptions.

    Mirrors the table in ``docs/design.md`` § Tools § Status-code
    mapping. Anything 2xx falls through silently; 401/403 are folded
    into :class:`ServerError` for v1 (the auth path is locked at T2
    of the prior map; the bridge verifies the inbound token, so an
    outbound 401 means the SB ``SB_AUTH_TOKEN`` drifted from the
    bridge's and the operator needs to investigate).

    T43: 5xx responses additionally call :func:`_parse_cf_error` on
    the response body; when the body looks CF-shaped, the parsed
    hint is attached to the raised :class:`ServerError` as
    ``cf_hint``. The MCP tool layer then surfaces ``cf_hint`` on
    the error envelope so an agent can decide whether to retry
    (matching the ``retry_after`` value CF publishes) without
    pattern-matching the raw CF JSON. Non-CF 5xx bodies (SB's
    own error format, plain HTML from a reverse proxy, empty
    body) leave ``cf_hint=None`` and the error envelope is
    unchanged from the pre-T43 shape — no new field on the wire,
    no breakage for non-CF deployments.
    """
    status = response.status_code
    if 200 <= status < 300:
        return
    if status == 404:
        raise PageNotFound(f"page not found: {response.request.url}")
    if status == 412:
        raise PreconditionFailed(
            f"precondition failed: {response.request.url}"
        )
    if status == 413:
        raise BodyTooLarge(
            f"body too large: limit is {_BODY_LIMIT_BYTES // (1024 * 1024)} MiB"
        )
    if status >= 500:
        exc = ServerError(f"silverbullet error: {status}")
        exc.cf_hint = _parse_cf_error(response.text)
        raise exc
    # 4xx we don't model explicitly — treat as a server-side
    # configuration drift (likely auth-related) and surface as 5xx.
    exc = ServerError(f"silverbullet error: {status}")
    exc.cf_hint = _parse_cf_error(response.text)
    raise exc
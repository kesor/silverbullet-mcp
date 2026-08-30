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
- :func:`list_pages` — ``GET /.fs`` — still returns
  ``list[FileMeta]`` (the minimal subset) until T28 widens both
  client and tool to ``list[PageMeta]``.

The status-code mapping lives in :func:`_raise_for_status` and matches
the table in ``docs/design.md`` § Tools. The PUT envelope carries the
full header set the design doc § SilverBullet client contract calls
out for writes (``X-Source: external``, ``X-Permission: rw``,
``X-Created``, ``X-Last-Modified``, ``X-Content-Length``,
``Content-Type: text/markdown``, optional ``If-Match`` /
``If-None-Match``) so SB's attribution log distinguishes bridge
writes from editor / sync writes, and so future SB versions that
honor request-side meta can read it without a bridge change.

The GET and PUT response ``X-*`` headers (``X-Created``,
``X-Last-Modified``, ``X-Content-Length``) plus ``ETag`` flow into
:class:`PageMeta` via :func:`_meta_from_response`. Any missing /
malformed header becomes ``None`` (defensive parse via
:func:`_parse_int_header`) — same shape as the prior ``None`` ETag
handling on older SB / proxy-stripped responses.
"""

from __future__ import annotations

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
    """SB returned a 5xx status. Carries the status code."""


@dataclass(frozen=True)
class FileMeta:
    """Subset of SB's ``FileMeta`` we actually surface.

    SB returns more fields (``createdAt``, ``lastModified``, ``size``,
    ``contentType``); v1 only needs ``name`` for ``list_pages`` and
    ``etag`` for the optional ``If-Match`` round-trip. v1.2's T28
    will widen this to the full :class:`PageMeta` shape; until then
    it stays the minimal subset.
    """

    name: str
    etag: str | None = None


@dataclass(frozen=True)
class PageMeta:
    """Single-source-of-truth acknowledgement shape for read + write tools.

    v1.2 T23 (write tools) and T24 (read tools) both return this shape
    so an agent that just made a write knows ``size_bytes`` /
    ``last_modified_ms`` / ``created_ms`` without a follow-up read, and
    a read returns the same envelope so a caller doesn't have to learn
    two shapes. T28 widens :class:`FileMeta` to this same shape so
    ``list_pages`` also returns ``list[PageMeta]`` — one envelope, every
    tool.

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

    async def list_pages(self) -> list[FileMeta]:
        """Return ``FileMeta`` for every page in the space.

        SB returns a JSON array of ``FileMeta`` objects (**only** when
        the request carries ``X-Sync-Mode`` — without it, SB
        307-redirects ``GET /.fs`` to the SPA UI). v1 only threads
        ``name`` and ``etag`` through to MCP; the rest is ignored
        (avoids a Pydantic model we'd have to keep in sync with the
        upstream server). v1.2 T28 widens the tool-shape return to
        ``list[PageMeta]`` (full meta); the client-side
        ``list_pages`` stays returning ``FileMeta`` (the minimal
        subset) until T28, then both client and tool widen together.
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
        # The space's ``/.fs`` list payload actually carries
        # ``created`` / ``lastModified`` / ``contentType`` / ``size``
        # / ``perm`` per ``server/src/handlers/fs.rs`` — v1 only
        # surfaces ``name`` and ``etag``, so the rest is dropped
        # here. ``etag`` is ``None`` on this SB build (it isn't
        # included in the sync-mode list payload), which means
        # ``write_page(..., if_match=<etag>)`` has no round-trip path
        # until SB starts emitting an ``etag`` field; the
        # ``list_pages`` tool still surfaces every name, which is
        # what the tool consumer wants in v1. T28 widens this and
        # adds an opt-in ``MCP_SILVERBULLET_LIST_PAGES_HYDRATE_ETAGS``
        # env var so the operator who needs etag round-trips pays the
        # N+1 cost.
        return [
            FileMeta(name=item["name"], etag=item.get("etag"))
            for item in data
            if isinstance(item, dict) and "name" in item
        ]


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
        etag=response.headers.get("ETag"),
        size_bytes=_parse_int_header(response.headers.get("X-Content-Length")),
        last_modified_ms=_parse_int_header(response.headers.get("X-Last-Modified")),
        created_ms=_parse_int_header(response.headers.get("X-Created")),
        body=body,
    )


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


def _raise_for_status(response: httpx.Response) -> None:
    """Translate SB HTTP statuses to typed exceptions.

    Mirrors the table in ``docs/design.md`` § Tools § Status-code
    mapping. Anything 2xx falls through silently; 401/403 are folded
    into :class:`ServerError` for v1 (the auth path is locked at T2
    of the prior map; the bridge verifies the inbound token, so an
    outbound 401 means the SB ``SB_AUTH_TOKEN`` drifted from the
    bridge's and the operator needs to investigate).
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
        raise ServerError(f"silverbullet error: {status}")
    # 4xx we don't model explicitly — treat as a server-side
    # configuration drift (likely auth-related) and surface as 5xx.
    raise ServerError(f"silverbullet error: {status}")
"""httpx2 adapter for the SilverBullet `/.fs/...` HTTP API.

The contract is read straight from the upstream SB server (see
``docs/design.md`` § SilverBullet client contract). This module is the
outbound half of the bridge; ``server.py`` calls into it from inside the
MCP tool handlers and translates the exceptions to ``ToolError`` for the
MCP wire.

Three entry points:

- :func:`read_page` — ``GET /.fs/{name}``
- :func:`write_page` — ``PUT /.fs/{name}``
- :func:`list_pages` — ``GET /.fs``

The status-code mapping lives in :func:`_raise_for_status` and matches
the table in ``docs/design.md`` § Tools. The PUT envelope carries the
full header set the design doc § SilverBullet client contract calls
out for writes (``X-Source: external``, ``X-Permission: rw``,
``X-Created``, ``X-Last-Modified``, ``X-Content-Length``,
``Content-Type: text/markdown``, optional ``If-Match`` /
``If-None-Match``) so SB's attribution log distinguishes bridge
writes from editor / sync writes, and so future SB versions that
honor request-side meta can read it without a bridge change.
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
    ``etag`` for the optional ``If-Match`` round-trip.
    """

    name: str
    etag: str | None = None


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

    async def read_page(self, name: str) -> str:
        """Return the markdown body of ``/.fs/{name}``.

        Raises :class:`PageNotFound` if the page is missing.
        """
        response = await self._client.get(f"/.fs/{name}")
        _raise_for_status(response)
        return response.text

    async def write_page(
        self,
        name: str,
        content: str,
        *,
        if_match: str | None = None,
        if_none_match: bool = False,
    ) -> str | None:
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
        str | None
            The new ETag for the body, taken from the response
            ``ETag`` header, or ``None`` if the response didn't carry
            one. SB always emits ``ETag`` on a successful write, so
            ``None`` is a contract drift worth logging about (older
            or misconfigured SB proxies may strip the header).

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
        return response.headers.get("ETag")

    async def list_pages(self) -> list[FileMeta]:
        """Return ``FileMeta`` for every page in the space.

        SB returns a JSON array of ``FileMeta`` objects (**only** when
        the request carries ``X-Sync-Mode`` — without it, SB
        307-redirects ``GET /.fs`` to the SPA UI). v1 only threads
        ``name`` and ``etag`` through to MCP; the rest is ignored
        (avoids a Pydantic model we'd have to keep in sync with the
        upstream server).
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
        # what the tool consumer wants in v1.
        return [
            FileMeta(name=item["name"], etag=item.get("etag"))
            for item in data
            if isinstance(item, dict) and "name" in item
        ]


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
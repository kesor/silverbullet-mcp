"""Inbound HTTP debug helpers for uvicorn's opaque parser warnings.

Uvicorn logs ``WARNING: Invalid HTTP request received.`` when httptools
cannot parse the first bytes of a connection. On the version we ship
(mcp==2.1.1's uvicorn) that warning carries **no** client address, no
parser error, and no payload preview — even at ``log_level=debug``.
The httptools exception is swallowed. ``UVICORN_LOG_LEVEL=debug``
therefore cannot diagnose the warning; the operator needs a hook
below uvicorn.

This module:

- classifies the first bytes (HTTP/1 request-line, HTTP/2 preface,
  TLS ClientHello, empty, other) so a Cloudflare-tunnel HTTP/2 or
  HTTPS-to-HTTP-port mistake is obvious from one log line;
- wraps uvicorn's ``HttpToolsProtocol.data_received`` so the
  classification + client + hex/ascii preview land at WARNING
  *before* uvicorn's unhelpful line;
- provides a Starlette middleware that dumps sanitized request
  metadata for connections that *did* parse as HTTP.

Bearer / Cookie / CF-Access JWT headers are redacted. Enabled only
when ``MCP_SILVERBULLET_LOG_LEVEL=debug``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from starlette.types import ASGIApp, Receive, Scope, Send

_LOG = logging.getLogger("mcp_silverbullet.http")

# HTTP/2 connection preface (RFC 9113 §3.4). cloudflared / CF
# orange-cloud HTTP/2 to origin is the #1 cause of uvicorn's
# "Invalid HTTP request received" on an HTTP/1.1-only bind.
_HTTP2_PREFACE = b"PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n"

# TLS record type 0x16 (handshake) + version 0x03 0x0x (SSL3/TLS).
# Seeing this on the MCP port means something is speaking HTTPS at
# an HTTP listener (wrong origin protocol in cloudflared).
_TLS_HANDSHAKE = 0x16

_HTTP_METHODS = (
    b"GET ",
    b"POST ",
    b"PUT ",
    b"HEAD ",
    b"PATCH ",
    b"DELETE ",
    b"OPTIONS ",
    b"CONNECT ",
    b"TRACE ",
)

_REDACT_HEADERS = frozenset(
    {
        "authorization",
        "cookie",
        "set-cookie",
        "cf-access-jwt-assertion",
        "cf-access-token",
        "x-forwarded-authorization",
    }
)

_PREVIEW_BYTES = 200


def classify_inbound(data: bytes) -> str:
    """Return a short label for the first bytes of a TCP payload."""
    if not data:
        return "empty"
    if data.startswith(_HTTP2_PREFACE) or data.startswith(b"PRI * HTTP/2.0"):
        return "http2-preface"
    if data[0] == _TLS_HANDSHAKE and len(data) > 2 and data[1] == 0x03:
        return "tls-clienthello"
    if data.startswith(_HTTP_METHODS):
        return "http1-request"
    if data.startswith(b"\x00") or data.startswith(b"\x16"):
        return "binary-non-http"
    return "unknown"


def preview_bytes(data: bytes, limit: int = _PREVIEW_BYTES) -> str:
    """ASCII-safe repr of the first ``limit`` bytes for log lines."""
    chunk = data[:limit]
    return repr(chunk) + ("…" if len(data) > limit else "")


def redact_headers(headers: list[tuple[bytes, bytes]]) -> dict[str, str]:
    """ASGI header list → dict with secrets replaced by ``<redacted>``."""
    out: dict[str, str] = {}
    for raw_name, raw_value in headers:
        name = raw_name.decode("latin-1", errors="replace").lower()
        if name in _REDACT_HEADERS:
            out[name] = "<redacted>"
        else:
            out[name] = raw_value.decode("latin-1", errors="replace")
    return out


def _client_addr(protocol: Any) -> str:  # noqa: ANN401
    client = getattr(protocol, "client", None)
    if client:
        return f"{client[0]}:{client[1]}"
    return "unknown"


def _wrap_data_received(cls: type, orig: Callable[..., None]) -> None:
    """Log first-bytes classification, then call uvicorn's handler."""

    def data_received(self: Any, data: bytes) -> None:  # noqa: ANN401
        kind = classify_inbound(data)
        client = _client_addr(self)
        # Always log at DEBUG so a quiet-looking service still
        # shows *something* on every TCP payload. Non-HTTP/1 is
        # WARNING so it sits next to uvicorn's opaque line.
        _LOG.debug(
            "inbound %s from %s (%d bytes): %s",
            kind,
            client,
            len(data),
            preview_bytes(data),
        )
        if kind != "http1-request":
            _LOG.warning(
                "non-HTTP/1 inbound from %s (%s, %d bytes): %s",
                client,
                kind,
                len(data),
                preview_bytes(data),
            )
        orig(self, data)

    data_received._mcp_sb_wrapped = True  # type: ignore[attr-defined]
    cls.data_received = data_received  # type: ignore[method-assign]


def install_http_debug_hooks() -> None:
    """Wrap uvicorn HTTP/1 implementations to log unparseable payloads.

    This uvicorn build defaults to **h11** when httptools isn't
    installed (the nix/uv env we ship). Wrapping only httptools
    is a no-op in that case — the operator still sees uvicorn's
    empty ``Invalid HTTP request received`` line. Hook both.

    Safe to call more than once (idempotent).
    """
    hooked: list[str] = []
    try:
        from uvicorn.protocols.http.httptools_impl import HttpToolsProtocol
    except ImportError:
        HttpToolsProtocol = None  # type: ignore[misc, assignment]
    else:
        if not getattr(HttpToolsProtocol.data_received, "_mcp_sb_wrapped", False):
            _wrap_data_received(HttpToolsProtocol, HttpToolsProtocol.data_received)
            hooked.append("httptools")

    try:
        from uvicorn.protocols.http.h11_impl import H11Protocol
    except ImportError:
        H11Protocol = None  # type: ignore[misc, assignment]
    else:
        if not getattr(H11Protocol.data_received, "_mcp_sb_wrapped", False):
            _wrap_data_received(H11Protocol, H11Protocol.data_received)
            hooked.append("h11")
        if not getattr(H11Protocol.handle_events, "_mcp_sb_wrapped", False):
            _wrap_h11_handle_events(H11Protocol)
            hooked.append("h11.handle_events")

    if hooked:
        _LOG.info(
            "installed inbound dump hooks (%s); non-HTTP/1 first-bytes "
            "log as mcp_silverbullet.http WARNING",
            ", ".join(hooked),
        )
    else:
        _LOG.warning(
            "could not hook uvicorn HTTP protocols; Invalid HTTP request "
            "lines will stay opaque"
        )


def _wrap_h11_handle_events(cls: type) -> None:
    """Surface h11's RemoteProtocolError next to uvicorn's empty warning."""
    import h11

    orig = cls.handle_events

    def handle_events(self: Any) -> None:  # noqa: ANN401
        conn = self.conn
        orig_next = conn.next_event

        def next_event() -> Any:  # noqa: ANN401
            try:
                return orig_next()
            except h11.RemoteProtocolError as exc:
                _LOG.warning(
                    "h11 rejected request from %s: %s",
                    _client_addr(self),
                    exc,
                )
                raise

        conn.next_event = next_event
        try:
            orig(self)
        finally:
            conn.next_event = orig_next

    handle_events._mcp_sb_wrapped = True  # type: ignore[attr-defined]
    cls.handle_events = handle_events  # type: ignore[method-assign]


class RequestDumpMiddleware:
    """Log method/path/status/sanitized headers for parsed HTTP requests."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(
        self, scope: Scope, receive: Receive, send: Send
    ) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        method = scope.get("method", "?")
        path = scope.get("path", "?")
        query = scope.get("query_string", b"").decode("latin-1", errors="replace")
        client = scope.get("client")
        client_s = f"{client[0]}:{client[1]}" if client else "unknown"
        http_version = scope.get("http_version", "?")
        headers = redact_headers(list(scope.get("headers") or []))
        _LOG.debug(
            "http %s %s%s v%s from %s headers=%s",
            method,
            path,
            f"?{query}" if query else "",
            http_version,
            client_s,
            headers,
        )

        status_holder: dict[str, int] = {}

        async def send_wrapped(message: dict[str, Any]) -> None:
            if message.get("type") == "http.response.start":
                status_holder["status"] = int(message.get("status", 0))
            await send(message)

        try:
            await self.app(scope, receive, send_wrapped)
        finally:
            _LOG.debug(
                "http %s %s -> %s from %s",
                method,
                path,
                status_holder.get("status", "-"),
                client_s,
            )

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


def install_httptools_debug_hook() -> None:
    """Wrap uvicorn's httptools ``data_received`` to log unparseable payloads.

    Safe to call more than once (idempotent). No-op if uvicorn's
    httptools protocol isn't importable (h11-only install).
    """
    try:
        from uvicorn.protocols.http.httptools_impl import HttpToolsProtocol
    except ImportError:
        _LOG.debug("httptools protocol not installed; skip inbound dump hook")
        return
    if getattr(HttpToolsProtocol.data_received, "_mcp_sb_wrapped", False):
        return
    orig = HttpToolsProtocol.data_received

    def data_received(self: Any, data: bytes) -> None:  # noqa: ANN401
        kind = classify_inbound(data)
        if kind != "http1-request":
            client = (
                f"{self.client[0]}:{self.client[1]}"
                if getattr(self, "client", None)
                else "unknown"
            )
            # WARNING (not DEBUG) so a single ``LOG_LEVEL=debug`` boot
            # still surfaces the classification next to uvicorn's
            # "Invalid HTTP request received" line. http1-request
            # chunks stay quiet here — the ASGI middleware covers
            # those.
            _LOG.warning(
                "non-HTTP/1 inbound from %s (%s, %d bytes): %s",
                client,
                kind,
                len(data),
                preview_bytes(data),
            )
        orig(self, data)

    data_received._mcp_sb_wrapped = True  # type: ignore[attr-defined]
    HttpToolsProtocol.data_received = data_received  # type: ignore[method-assign]
    _LOG.info(
        "installed httptools inbound dump; non-HTTP/1 first-bytes "
        "will log as mcp_silverbullet.http WARNING"
    )


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

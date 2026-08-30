"""Unit tests for inbound HTTP debug classification + header redaction."""

from __future__ import annotations

from mcp_silverbullet.http_debug import (
    classify_inbound,
    install_http_debug_hooks,
    preview_bytes,
    redact_headers,
)


def test_classify_http2_preface() -> None:
    preface = b"PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n"
    assert classify_inbound(preface) == "http2-preface"
    assert classify_inbound(b"PRI * HTTP/2.0\r\n") == "http2-preface"


def test_classify_tls_clienthello() -> None:
    # TLS handshake record: type 0x16, version 0x03 0x03 (TLS 1.2).
    payload = bytes([0x16, 0x03, 0x03, 0x00, 0x01, 0x00])
    assert classify_inbound(payload) == "tls-clienthello"


def test_classify_http1() -> None:
    assert classify_inbound(b"POST /mcp HTTP/1.1\r\n") == "http1-request"
    assert classify_inbound(b"GET / HTTP/1.1\r\n") == "http1-request"


def test_classify_empty_and_unknown() -> None:
    assert classify_inbound(b"") == "empty"
    assert classify_inbound(b"\x00\x00\x00") == "binary-non-http"
    assert classify_inbound(b"hello") == "unknown"


def test_redact_authorization_and_cf_access_jwt() -> None:
    headers = [
        (b"host", b"bridge.test"),
        (b"authorization", b"Bearer super-secret"),
        (b"cf-access-jwt-assertion", b"eyJhbGciOiJSUzI1NiJ9.aaa"),
        (b"user-agent", b"curl/8.0"),
    ]
    out = redact_headers(headers)
    assert out["host"] == "bridge.test"
    assert out["authorization"] == "<redacted>"
    assert out["cf-access-jwt-assertion"] == "<redacted>"
    assert out["user-agent"] == "curl/8.0"


def test_preview_truncates() -> None:
    blob = b"x" * 500
    text = preview_bytes(blob, limit=10)
    assert text.endswith("…")
    assert "xxxxxxxxxx" in text or "x" * 10 in text


def test_install_hooks_wraps_h11() -> None:
    """This env ships uvicorn with h11, not httptools."""
    from uvicorn.protocols.http.h11_impl import H11Protocol

    install_http_debug_hooks()
    assert getattr(H11Protocol.data_received, "_mcp_sb_wrapped", False)
    install_http_debug_hooks()  # idempotent

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


def test_preview_redacts_long_bearer_token() -> None:
    """Long tokens get prefix/suffix trace; short tokens are <redacted>."""
    import ast

    token = b"eyJhbGciOiJSUzI1NiIsImtpZCI6Ijc0OTc5YzgxYmIzZWM4ZjU3NTllMjY2MzRlYzIwNGQ2MGE1Nzk4MmMyZjE1ZjkxYWQxNjExM2ExY2QzZjg3NDcifQ.fake-sig"
    request = (
        b"POST /mcp HTTP/1.1\r\n"
        b"Host: bridge\r\n"
        b"Authorization: Bearer " + token + b"\r\n"
        b"Content-Length: 0\r\n"
        b"\r\n"
    )
    out = preview_bytes(request, limit=10_000)
    # ``preview_bytes`` returns ``repr(scrubbed_bytes)``. Use
    # ``ast.literal_eval`` to recover the underlying bytes
    # rather than substring-matching the escaped string form.
    out_bytes = ast.literal_eval(out)
    assert isinstance(out_bytes, bytes)
    # Header name kept verbatim so the operator sees what was redacted.
    assert b"Authorization:" in out_bytes
    # Token's first 8 + ellipsis + last 4 chars survive.
    assert token[:8] in out_bytes
    assert b"\xe2\x80\xa6" in out_bytes  # utf-8 for "…"
    assert token[-4:] in out_bytes
    # The middle of the token does NOT leak.
    assert token[10:-5] not in out_bytes


def test_preview_redacts_short_bearer_token() -> None:
    """Short tokens collapse to ``<redacted>`` (no useful prefix/suffix)."""
    import ast

    request = b"GET / HTTP/1.1\r\nAuthorization: Bearer abc\r\n\r\n"
    out = preview_bytes(request)
    out_bytes = ast.literal_eval(out)
    assert isinstance(out_bytes, bytes)
    assert b"Bearer abc" not in out_bytes
    assert b"<redacted>" in out_bytes


def test_preview_redacts_cf_access_jwt_header() -> None:
    import ast

    request = (
        b"GET / HTTP/1.1\r\n"
        b"CF-Access-Jwt-Assertion: " + (b"X" * 60) + b"\r\n\r\n"
    )
    out = preview_bytes(request)
    out_bytes = ast.literal_eval(out)
    assert isinstance(out_bytes, bytes)
    assert b"CF-Access-Jwt-Assertion:" in out_bytes
    # The full token must NOT appear together. Prefix/suffix trace
    # is OK (8 X's + ellipsis + 4 X's), but the middle is gone.
    assert b"X" * 60 not in out_bytes
    assert b"X" * 20 not in out_bytes  # no contiguous run of the secret


def test_preview_keeps_non_sensitive_headers_verbatim() -> None:
    import ast

    request = (
        b"POST /mcp HTTP/1.1\r\n"
        b"User-Agent: curl/8.0.0\r\n"
        b"Content-Type: application/json\r\n"
        b"\r\n"
    )
    out = preview_bytes(request)
    out_bytes = ast.literal_eval(out)
    assert isinstance(out_bytes, bytes)
    assert b"User-Agent: curl/8.0.0" in out_bytes
    assert b"Content-Type: application/json" in out_bytes


def test_install_hooks_wraps_h11() -> None:
    """This env ships uvicorn with h11, not httptools."""
    from uvicorn.protocols.http.h11_impl import H11Protocol

    install_http_debug_hooks()
    assert getattr(H11Protocol.data_received, "_mcp_sb_wrapped", False)
    install_http_debug_hooks()  # idempotent

"""Layer 2 tests for ``server.py``: HTTP wire-level auth + discovery.

The bridge is exercised as a real ASGI app on a real socket — just
without a TCP stack. ``httpx.ASGITransport`` forwards HTTP requests to
the Starlette app exactly the way a network would: every middleware
runs (BearerAuthBackend, AuthContextMiddleware, RequireAuthMiddleware,
the StreamableHTTPSessionManager, the DNS-rebinding transport-security
filter), every header is parsed, every status code and body shape is
what a Grok client would see. The only thing skipped is the OS socket
plumbing — no port allocation, no flake, no `time.sleep` to let uvicorn
warm up.

The map's T5 ticket calls for ``streamable_http_app() + uvicorn``; ASGI
transport satisfies the *intent* (real wire behaviour on the inbound
hop) without the test-suite cost of a real port. The T7 live-SB test
will drive uvicorn for real against the dev-box SilverBullet; T5 is
the unit-level guard.

Coverage:

- ``POST /mcp`` without an ``Authorization`` header → ``401`` +
  ``WWW-Authenticate: Bearer error="invalid_token",
  error_description="...", resource_metadata="..."``.
- ``POST /mcp`` with a wrong token → same ``401`` shape. Verifier never
  leaks whether the *header* was malformed vs. the token was wrong.
- ``GET /.well-known/oauth-protected-resource/mcp`` (no auth) → ``200``
  with ``resource``, ``authorization_servers`` (pointing at the
  resource URL — no separate authz server, T2 of the prior map),
  ``bearer_methods_supported=["header"]`` (only the header method, per
  SDK). ``scopes_supported`` is omitted (``AuthSettings.required_scopes``
  is unset, so the Pydantic model's ``exclude_none=True`` strips it).
- ``POST /mcp`` with auth but ``Accept: text/plain`` → ``406`` from the
  StreamableHTTP handler. The handler sits behind the auth middleware,
  so this only fires *with* a valid token; that's route parity, not
  route precedence.
- Full initialize + ``call_tool("read_page")`` roundtrip over HTTP via
  ``streamable_http_client`` + ``ClientSession`` to confirm the wire
  path actually serves our tools.

The bridge listens on the loopback default (``127.0.0.1``); the host
header forwarded by ``ASGITransport`` is ``bridge.test`` so the SDK's
auto-enabled DNS-rebinding protection doesn't reject it.
"""

from __future__ import annotations

import httpx2 as httpx
import pytest
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver.server import MCPServer

from mcp_silverbullet.sb_client import SBClient
from mcp_silverbullet.server import build_mcp
from mcp_silverbullet.verifier import StaticTokenVerifier


TOKEN = "test-secret-do-not-use-in-prod"
RESOURCE_URL = "http://bridge.test/mcp"
# The host the SDK uses to auto-enable DNS-rebinding protection is the
# ``host`` argument to ``streamable_http_app``; passing
# ``host="bridge.test"`` (anything *other* than 127.0.0.1/localhost/::1)
# bypasses the protection, so the test can use a non-loopback ``Host``
# header without the 421 "Invalid Host" rejection. The real bridge
# (T6) keeps the loopback default and adds the tunnel host via
# ``MCP_SILVERBULLET_ALLOWED_HOSTS`` (standing preference in the map).
APP_HOST = "bridge.test"


def _build_app(handler) -> MCPServer:
    """Build a fully-wired bridge whose SB transport is ``handler``.

    ``handler`` is the same ``httpx.MockTransport`` trick used in
    Layer 1 (``tests/test_tools_in_memory.py``): every HTTP request the
    bridge makes to SilverBullet goes to ``handler``, so the test never
    needs a real SB. The whole inbound half (auth middleware,
    StreamableHTTP handler, JSON-RPC dispatch) runs for real against
    the ASGI app.
    """
    sb = SBClient.__new__(SBClient)
    sb._client = httpx.AsyncClient(
        base_url="http://sb.test",
        headers={"Authorization": f"Bearer {TOKEN}"},
        transport=httpx.MockTransport(handler),
    )
    return build_mcp(sb, token=TOKEN, resource_url=RESOURCE_URL)


def _wire_app(handler) -> MCPServer:
    """Return the bridge ASGI app, with the SDK's auto-lifespan driven.

    ``httpx.ASGITransport`` doesn't drive Starlette's lifespan protocol
    (no ``lifespan.startup`` / ``lifespan.shutdown`` messages); the
    ``StreamableHTTPSessionManager`` requires its ``run()`` context to
    be active before it accepts requests, so we open it manually before
    yielding the app. Each test that talks to ``/mcp`` enters this
    helper's context; tests that only hit the discovery doc or do a
    ``POST`` that gets rejected at the auth middleware can skip it.
    """
    mcp = _build_app(handler)
    return mcp.streamable_http_app(host=APP_HOST)


# --- 401 + WWW-Authenticate shape --------------------------------------


@pytest.mark.asyncio
async def test_post_without_authorization_returns_401_with_resource_metadata() -> None:
    """No header at all → 401 + WWW-Authenticate referencing the metadata doc.

    The ``resource_metadata`` URL is the one the discovery doc is
    served at, derived by ``build_resource_metadata_url`` from
    ``resource_server_url`` (``http://bridge.test/mcp`` →
    ``http://bridge.test/.well-known/oauth-protected-resource/mcp``).
    A Grok client seeing this header knows where to fetch the OAuth
    dance config; v1 skips the dance (static bearer), but the header
    shape is the same.
    """
    app = _wire_app(lambda req: httpx.Response(200, text="should not be called"))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://bridge.test",
    ) as client:
        response = await client.post(
            "/mcp",
            headers={
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
            },
            content=b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}',
        )

    assert response.status_code == 401
    www_auth = response.headers.get("WWW-Authenticate", "")
    assert www_auth.startswith("Bearer ")
    assert 'error="invalid_token"' in www_auth
    assert 'resource_metadata="http://bridge.test/.well-known/oauth-protected-resource/mcp"' in www_auth


@pytest.mark.asyncio
async def test_post_with_wrong_token_returns_401_with_same_shape() -> None:
    """Wrong token → 401 indistinguishable from no header.

    The verifier collapses both cases to ``invalid_token``; a
    timing-side-channel observer gets nothing extra from a wrong token
    vs. a missing header (the constant-time compare still runs on
    equal-length byte strings, and ``hmac.compare_digest`` returns
    ``False`` early if lengths differ — but the *response* is identical
    in shape, which is what matters for a probing client).
    """
    app = _wire_app(lambda req: httpx.Response(200, text="should not be called"))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://bridge.test",
    ) as client:
        response = await client.post(
            "/mcp",
            headers={
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
                "Authorization": "Bearer not-the-real-token",
            },
            content=b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}',
        )

    assert response.status_code == 401
    www_auth = response.headers.get("WWW-Authenticate", "")
    assert www_auth.startswith("Bearer ")
    assert 'error="invalid_token"' in www_auth
    assert 'resource_metadata="http://bridge.test/.well-known/oauth-protected-resource/mcp"' in www_auth


# --- /.well-known/oauth-protected-resource/mcp ------------------------


@pytest.mark.asyncio
async def test_discovery_doc_returns_rfc_9728_metadata() -> None:
    """The protected-resource metadata document is served unauthenticated.

    RFC 9728 §3 says the metadata endpoint must be reachable without an
    access token; that's how a client bootstraps the auth dance. v1
    uses a static bearer (no dance), but the SDK still mounts the
    document because ``AuthSettings`` is configured — and a Grok
    client that fetches the doc to *check* what scopes are supported
    should see a stable shape.
    """
    app = _wire_app(lambda req: httpx.Response(200, text="unused"))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://bridge.test",
    ) as client:
        response = await client.get("/.well-known/oauth-protected-resource/mcp")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    doc = response.json()
    assert doc["resource"] == RESOURCE_URL
    # v1 has no separate authz server (T2 of the prior map); the
    # bridge is its own issuer, so ``authorization_servers`` lists the
    # resource URL. The SDK takes both ``issuer_url`` and
    # ``resource_server_url`` and stamps ``issuer_url`` here.
    assert doc["authorization_servers"] == [RESOURCE_URL]
    assert doc["bearer_methods_supported"] == ["header"]
    # ``scopes_supported`` is omitted: ``AuthSettings.required_scopes``
    # is unset, and the SDK uses ``model_dump_json(exclude_none=True)``
    # to strip ``None`` fields (see ``PydanticJSONResponse.render``).
    # Asserting on the absence keeps the test honest about the shape.
    assert "scopes_supported" not in doc


# --- Accept header route parity ----------------------------------------


@pytest.mark.asyncio
async def test_post_with_wrong_accept_header_returns_406() -> None:
    """Auth passes but Accept is wrong → 406 from the streamable handler.

    The auth middleware (front) doesn't check Accept; the
    StreamableHTTP handler (behind) requires
    ``application/json, text/event-stream`` for an SSE response. The
    SDK's error message is the canonical "Not Acceptable: Client must
    accept both application/json and text/event-stream" — we assert on
    that wording so a future SDK message change is caught loudly.
    """
    app = _wire_app(lambda req: httpx.Response(200, text="should not be called"))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://bridge.test",
        ) as client:
            response = await client.post(
                "/mcp",
                headers={
                    "Accept": "text/plain",
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {TOKEN}",
                },
                content=b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}',
            )

    assert response.status_code == 406
    assert "application/json" in response.text
    assert "text/event-stream" in response.text


# --- Happy-path tool call over real HTTP -------------------------------


@pytest.mark.asyncio
async def test_full_initialize_then_read_page_roundtrip_over_http() -> None:
    """End-to-end: ``streamable_http_client`` + ``ClientSession`` calls ``read_page``.

    Confirms that the inbound hop (auth middleware → session manager →
    JSON-RPC dispatch → tool handler → SB client → response) wires up
    the same way it will for a real Grok client over a real tunnel.
    The SB side is mocked via ``httpx.MockTransport`` (Layer 3's
    concern — wire envelope is what ``test_sb_client.py`` covers;
    here we just need a 200 with a body).
    """
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/.fs/index"
        return httpx.Response(200, text="# hello over the wire")

    app = _wire_app(handler)
    async with app.router.lifespan_context(app):
        http_client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://bridge.test",
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        async with http_client:
            async with streamable_http_client(
                url=f"{RESOURCE_URL}",
                http_client=http_client,
            ) as (read, write):
                async with ClientSession(read, write) as session:
                    init_result = await session.initialize()
                    assert init_result.server_info.name == "mcp-silverbullet"

                    tools = await session.list_tools()
                    tool_names = sorted(t.name for t in tools.tools)
                    assert tool_names == [
                        "append_to_page",
                        "check_task",
                        # T32: refuse-to-overwrite create tool.
                        # Distinct from ``write_page`` (which
                        # overwrite-or-creates by default); the
                        # agent that has a specific create intent
                        # uses ``create_page`` and gets a clean
                        # ``page already exists`` ``ToolError`` on
                        # collision.
                        "create_page",
                        "delete_page",
                        "diff_pages",
                        "list_pages",
                        "list_tasks",
                        "move_page",
                        "page_exists",
                        "patch_page_lines",
                        "patch_page_replace",
                        # T33: top-of-body insert with YAML
                        # frontmatter awareness; mirrors
                        # ``append_to_page``'s
                        # read-modify-write + ``dry_run``
                        # shape. Distinct from ``append_to_page``
                        # (which inserts at the bottom).
                        "prepend_to_page",
                        "read_page",
                        "write_page",
                    ]

                    call_result = await session.call_tool(
                        "read_page", {"name": "index"}
                    )
                    assert call_result.is_error is False
                    # T24: ``read_page`` returns the ack envelope,
                    # not a raw string. The body lives under
                    # ``structured_content["body"]``; ``content[0].text``
                    # is a JSON-serialized copy of the same envelope.
                    assert call_result.structured_content == {
                        "body": "# hello over the wire",
                        "etag": None,
                        "size_bytes": None,
                        "last_modified_ms": None,
                    }


# --- JWT mode (v1.4) ---------------------------------------------------


@pytest.fixture
def jwks_server_url(monkeypatch):
    """Serve a one-key JWKS doc over an in-memory ``PyJWKClient`` patch.

    Mirrors the ``jwks_server_url`` fixture in
    ``test_verifier_jwt.py`` — extracted here so the HTTP
    wire tests don't have to import the verifier-internal
    fixtures. The returned URL is a placeholder; the
    monkey-patched ``PyJWKClient.fetch_data`` returns the
    in-memory JWKS regardless of which URL the verifier
    requests.
    """
    import base64
    import json

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    private_key = rsa.generate_private_key(
        public_exponent=65537, key_size=2048
    )
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    public_pem = (
        private_key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    numbers = (
        serialization.load_pem_public_key(public_pem.encode())
        .public_numbers()
    )

    def _b64u(value: int) -> str:
        raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    jwk = {
        "kty": "RSA",
        "kid": "test-key-1",
        "alg": "RS256",
        "use": "sig",
        "n": _b64u(numbers.n),
        "e": _b64u(numbers.e),
    }

    def fake_fetch_data(self) -> dict:
        return {"keys": [jwk]}

    monkeypatch.setattr(
        "jwt.PyJWKClient.fetch_data", fake_fetch_data, raising=True
    )
    return "https://test.local/.well-known/jwks.json", private_pem


def _build_jwt_app(jwks_url: str) -> MCPServer:
    """Build a bridge wired with a :class:`JWTVerifier` against the fake JWKS."""
    from mcp_silverbullet.verifier import JWTVerifier

    sb = SBClient.__new__(SBClient)
    sb._client = httpx.AsyncClient(
        base_url="http://sb.test",
        headers={"Authorization": "Bearer unused"},
        transport=httpx.MockTransport(
            lambda req: httpx.Response(200, text="placeholder")
        ),
    )
    mcp = build_mcp(
        sb,
        verifier=JWTVerifier(
            issuer="https://acme.cloudflareaccess.com",
            audience="00000000000000000000000000000000",
            jwks_url=jwks_url,
        ),
        resource_url=RESOURCE_URL,
    )
    return mcp.streamable_http_app(host=APP_HOST)


@pytest.mark.asyncio
async def test_jwt_mode_accepts_valid_jwt_and_rejects_expired(
    jwks_server_url,
) -> None:
    """End-to-end: a valid CF-Access-shaped JWT round-trips through the bridge.

    Mirrors the static-mode ``test_full_initialize_then_read_page_roundtrip_over_http``
    but in JWT mode: the bridge validates the bearer against
    the (mocked) JWKS, surfaces the principal, and serves the
    MCP wire. Asserts both the happy path (valid JWT → 200)
    and the rejection path (expired JWT → 401, same shape as
    any other verifier rejection).
    """
    import time

    import jwt as pyjwt

    jwks_url, private_pem = jwks_server_url
    app = _build_jwt_app(jwks_url)
    async with app.router.lifespan_context(app):
        now = int(time.time())
        valid_token = pyjwt.encode(
            {
                "iss": "https://acme.cloudflareaccess.com",
                "aud": "00000000000000000000000000000000",
                "sub": "user-uuid-1234",
                "iat": now,
                "exp": now + 300,
            },
            private_pem,
            algorithm="RS256",
            headers={"kid": "test-key-1"},
        )
        http_client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://bridge.test",
            headers={"Authorization": f"Bearer {valid_token}"},
        )
        async with http_client:
            async with streamable_http_client(
                url=f"{RESOURCE_URL}",
                http_client=http_client,
            ) as (read, write):
                async with ClientSession(read, write) as session:
                    init = await session.initialize()
                    assert init.server_info.name == "mcp-silverbullet"
                    tools = await session.list_tools()
                    # The 14-tool surface is the same in JWT mode
                    # as in static mode — auth mode doesn't gate
                    # tools, just the principal.
                    assert len(tools.tools) == 14

            # Rejection path: expired JWT. ``exp`` 60s past, default
            # leeway is 30s, so this is firmly outside the window.
            expired_token = pyjwt.encode(
                {
                    "iss": "https://acme.cloudflareaccess.com",
                    "aud": "00000000000000000000000000000000",
                    "sub": "user-uuid-1234",
                    "iat": now - 600,
                    "exp": now - 60,
                },
                private_pem,
                algorithm="RS256",
                headers={"kid": "test-key-1"},
            )
            response = await http_client.post(
                "/mcp",
                headers={
                    "Accept": "application/json, text/event-stream",
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {expired_token}",
                },
                content=b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}',
            )
            assert response.status_code == 401
            www_auth = response.headers.get("WWW-Authenticate", "")
            assert 'error="invalid_token"' in www_auth

"""Layer-1 tests for the v1.4 JWT verifier.

The verifier's contract is small — ``async def verify_token(token) →
AccessToken | None`` — but the failure surface is large (every
PyJWT exception collapses to ``None``). These tests pin each of:

- The happy path: a valid RS256 token signed by a freshly-generated
  keypair with all required claims returns the right
  :class:`AccessToken` (``subject=<sub>``, ``client_id`` falls
  through to ``"cloudflare-access"`` for CF-Access-shaped tokens,
  ``claims`` carries the full dict, ``scopes`` is the v1.x
  ``("notes:read", "notes:write")`` tuple).

- Each rejection path: bad signature, expired, future-dated
  (``nbf``), wrong issuer, wrong audience, missing ``sub``,
  missing ``exp``, missing ``aud``, HS256 downgrade attempt.

- The :func:`select_verifier` factory: picks the right
  implementation, fails loud on misconfiguration.

The tests use an ephemeral in-process keypair so the JWKS is
served by a ``PyJWKClient`` pointed at a local ``PyJWKClient``-
flavored fixture, not at the network. That's faster and
deterministic; the live CF-Access JWKS rotation path is a
Layer-3 concern that lives in ``test_e2e_live_sb.py`` (if /
when we add one for JWT mode).
"""

from __future__ import annotations

import time

import jwt
import pytest

from mcp_silverbullet.verifier import (
    JWTVerifier,
    StaticTokenVerifier,
    _SCOPES,
    select_verifier,
)


# --- Fixture: ephemeral RS256 keypair --------------------------------


@pytest.fixture
def rsa_keypair() -> tuple[str, str]:
    """Generate an ephemeral RS256 keypair for the test session.

    Yields ``(private_pem, public_pem)`` as strings so the
    signer (private key) and verifier (public key) see the
    same cryptographic material without any disk round trip.
    """
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
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
    return private_pem, public_pem


@pytest.fixture
def jwks_server_url(rsa_keypair, monkeypatch) -> str:
    """Wire ``PyJWKClient`` to a fake JWKS endpoint serving the public key.

    The fixture registers a single kid (``test-key-1``) and
    patches ``PyJWKClient.fetch_data`` to return the in-memory
    JSON instead of HTTP-fetching. Keeps the verifier's
    production code path (cache → fetch → parse → match)
    intact while making the test fully synchronous.

    The PEM public key is converted to a JWK using
    ``cryptography``'s primitives (PyJWT's ``to_jwk`` only
    accepts a key object, not a PEM string, and PyJWT 2.x's
    ``from_jwk`` expects a JWK dict, not a PEM).
    """
    _private_pem, public_pem = rsa_keypair
    import base64
    import json

    from cryptography.hazmat.primitives import serialization as _ser

    pubkey = _ser.load_pem_public_key(public_pem.encode())
    numbers = pubkey.public_numbers()  # type: ignore[attr-defined]

    def _b64u(value: int) -> str:
        # JWK uses base64url without padding.
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
    # Note: PyJWT's real ``fetch_data`` parses the JSON response
    # via ``json.load`` and returns a ``dict``. The patched
    # version must return the same shape — bytes would make
    # ``get_jwk_set``'s ``isinstance(data, dict)`` check fail
    # with the misleading "did not return a JSON object"
    # error. (Caught this while debugging; the error message
    # is genuinely confusing because the response IS valid
    # JSON, just not in the form the cache expects.)
    jwks_doc = {"keys": [jwk]}

    def fake_fetch_data(self) -> dict:  # noqa: ANN001
        return jwks_doc

    monkeypatch.setattr(
        "jwt.PyJWKClient.fetch_data", fake_fetch_data, raising=True
    )
    return "https://test.local/.well-known/jwks.json"


@pytest.fixture
def verifier(jwks_server_url: str) -> JWTVerifier:
    """A :class:`JWTVerifier` pointed at the in-memory JWKS."""
    return JWTVerifier(
        issuer="https://acme.cloudflareaccess.com",
        audience="00000000000000000000000000000000",
        jwks_url=jwks_server_url,
    )


def _sign(private_pem: str, **claim_overrides) -> str:
    """Sign a JWT with the standard CF-Access claim set + overrides.

    Default claim set mirrors the shape CF Access puts in
    real tokens: ``iss`` / ``aud`` / ``sub`` / ``exp`` / ``iat`` /
    ``email`` / ``auth_status``. Tests override individual
    claims to drive the rejection paths.
    """
    now = int(time.time())
    claims = {
        "iss": "https://acme.cloudflareaccess.com",
        "aud": "00000000000000000000000000000000",
        "sub": "user-uuid-1234",
        "email": "[email protected]",
        "auth_status": "PASS",
        "iat": now,
        "exp": now + 300,
    }
    # ``claim_overrides with value ``None`` deletes the key
    # rather than setting it to ``None`` — lets a test like
    # the ``required_claims`` case omit ``email`` from the
    # signed token.
    for key, value in list(claim_overrides.items()):
        if value is None:
            claims.pop(key, None)
            del claim_overrides[key]
    claims.update(claim_overrides)
    return jwt.encode(claims, private_pem, algorithm="RS256", headers={"kid": "test-key-1"})


# --- Happy path -------------------------------------------------------


@pytest.mark.asyncio
async def test_valid_token_returns_access_token(
    verifier: JWTVerifier, rsa_keypair: tuple[str, str]
) -> None:
    """A well-formed RS256 token returns the principal on ``AccessToken``."""
    private_pem, _ = rsa_keypair
    token = _sign(private_pem)

    info = await verifier.verify_token(token)

    assert info is not None
    # ``subject`` is the CF Access user UUID — the principal
    # future per-user code branches on.
    assert info.subject == "user-uuid-1234"
    # ``client_id`` falls through to ``"cloudflare-access"``
    # for tokens without ``azp`` / ``client_id`` claims.
    assert info.client_id == "cloudflare-access"
    # Both scopes (the v1.x default — scope-gating is a
    # future ticket).
    assert info.scopes == list(_SCOPES)
    # ``claims`` carries the full decoded dict so downstream
    # code can read ``email`` / ``auth_status`` / etc.
    # without re-decoding the JWT.
    assert info.claims is not None
    assert info.claims["email"] == "[email protected]"
    assert info.claims["auth_status"] == "PASS"
    # ``expires_at`` round-trips from ``exp`` (epoch seconds).
    assert info.expires_at is not None
    assert info.expires_at == pytest.approx(int(time.time()) + 300, abs=5)


@pytest.mark.asyncio
async def test_azp_claim_used_for_client_id(
    verifier: JWTVerifier, rsa_keypair: tuple[str, str]
) -> None:
    """Auth0 / Okta OIDC tokens carry ``azp``; we surface it as ``client_id``."""
    private_pem, _ = rsa_keypair
    token = _sign(private_pem, azp="my-app-client-id")

    info = await verifier.verify_token(token)
    assert info is not None
    assert info.client_id == "my-app-client-id"


@pytest.mark.asyncio
async def test_client_id_claim_used_when_no_azp(
    verifier: JWTVerifier, rsa_keypair: tuple[str, str]
) -> None:
    """The OAuth-standard ``client_id`` claim is the second-priority source."""
    private_pem, _ = rsa_keypair
    token = _sign(private_pem, client_id="oauth-client-id")

    info = await verifier.verify_token(token)
    assert info is not None
    assert info.client_id == "oauth-client-id"


# --- Rejection paths --------------------------------------------------


@pytest.mark.asyncio
async def test_wrong_signature_returns_none(
    verifier: JWTVerifier, rsa_keypair: tuple[str, str]
) -> None:
    """A token signed by a *different* key fails with ``None``.

    The second keypair is generated fresh — no shared
    material with the verifier's JWKS — so the signature
    is invalid.
    """
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    other_private = (
        rsa.generate_private_key(public_exponent=65537, key_size=2048)
        .private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        .decode()
    )
    token = _sign(other_private)

    assert await verifier.verify_token(token) is None


@pytest.mark.asyncio
async def test_expired_token_returns_none(
    verifier: JWTVerifier, rsa_keypair: tuple[str, str]
) -> None:
    """``exp`` in the past → ``None``. A 60s past ``exp`` exceeds the default leeway."""
    private_pem, _ = rsa_keypair
    now = int(time.time())
    token = _sign(private_pem, iat=now - 600, exp=now - 60)

    assert await verifier.verify_token(token) is None


@pytest.mark.asyncio
async def test_expired_token_within_leeway_accepted(
    jwks_server_url: str, rsa_keypair: tuple[str, str]
) -> None:
    """An expired-but-within-leeway token still verifies.

    The 30s leeway is what lets a token that's nominally
    expired at ``exp - 0`` still verify if the verifier's
    clock is up to 30s ahead of the IdP's clock. The
    default leeway keeps CF Access's 30-minute tokens
    usable when the bridge host has modest NTP drift.
    """
    private_pem, _ = rsa_keypair
    verifier = JWTVerifier(
        issuer="https://acme.cloudflareaccess.com",
        audience="00000000000000000000000000000000",
        jwks_url=jwks_server_url,
        leeway_seconds=30,
    )
    now = int(time.time())
    # ``exp`` 5 seconds in the past — within leeway, accepted.
    token = _sign(private_pem, iat=now - 60, exp=now - 5)

    assert await verifier.verify_token(token) is not None


@pytest.mark.asyncio
async def test_future_nbf_returns_none(
    verifier: JWTVerifier, rsa_keypair: tuple[str, str]
) -> None:
    """``nbf`` in the future (beyond leeway) → ``None``."""
    private_pem, _ = rsa_keypair
    now = int(time.time())
    token = _sign(private_pem, nbf=now + 600)

    assert await verifier.verify_token(token) is None


@pytest.mark.asyncio
async def test_wrong_issuer_returns_none(
    verifier: JWTVerifier, rsa_keypair: tuple[str, str]
) -> None:
    """``iss`` mismatch → ``None``. The wrong-org token is rejected."""
    private_pem, _ = rsa_keypair
    token = _sign(
        private_pem, iss="https://attacker.example.com"
    )

    assert await verifier.verify_token(token) is None


@pytest.mark.asyncio
async def test_wrong_audience_returns_none(
    verifier: JWTVerifier, rsa_keypair: tuple[str, str]
) -> None:
    """``aud`` mismatch → ``None``. A token for another app fails."""
    private_pem, _ = rsa_keypair
    token = _sign(
        private_pem, aud="ffffffffffffffffffffffffffffffff"
    )

    assert await verifier.verify_token(token) is None


@pytest.mark.asyncio
async def test_missing_sub_returns_none(
    verifier: JWTVerifier, rsa_keypair: tuple[str, str]
) -> None:
    """Missing ``sub`` → ``None``. We require it (it's the principal)."""
    private_pem, _ = rsa_keypair
    claims = {
        "iss": "https://acme.cloudflareaccess.com",
        "aud": "00000000000000000000000000000000",
        # ``sub`` deliberately omitted.
        "iat": int(time.time()),
        "exp": int(time.time()) + 300,
    }
    token = jwt.encode(claims, private_pem, algorithm="RS256")

    assert await verifier.verify_token(token) is None


@pytest.mark.asyncio
async def test_missing_exp_returns_none(
    verifier: JWTVerifier, rsa_keypair: tuple[str, str]
) -> None:
    """Missing ``exp`` → ``None``. Tokens without an expiry are unsafe."""
    private_pem, _ = rsa_keypair
    now = int(time.time())
    claims = {
        "iss": "https://acme.cloudflareaccess.com",
        "aud": "00000000000000000000000000000000",
        "sub": "user-uuid-1234",
        "iat": now,
        # ``exp`` deliberately omitted.
    }
    token = jwt.encode(claims, private_pem, algorithm="RS256")

    assert await verifier.verify_token(token) is None


@pytest.mark.asyncio
async def test_hs256_downgrade_returns_none(
    jwks_server_url: str, rsa_keypair: tuple[str, str]
) -> None:
    """An HS256 token signed with the public key as the secret is rejected.

    The classic "algorithm confusion" attack: an attacker
    takes the public key (which everyone can see) and signs
    a forged token with it as an HMAC secret. PyJWT + the
    ``algorithms=["RS256"]`` allow-list refuses to accept
    the HS256-signed token even though the signature would
    technically verify against the public key as a shared
    secret.

    PyJWT 2.x's encode refuses to *create* an HS256 token
    with a PEM-formatted asymmetric key (a defensive check
    that protects developers from accidentally signing
    HS256 with their RSA private key). To simulate the
    attack anyway, we extract the modulus bytes from the
    public key (which is what the attacker would actually
    use as the HMAC secret — the raw key material, not the
    PEM wrapper) and sign an HS256 token by hand via the
    lower-level ``PyJWS.encode`` API (which takes raw
    bytes, not a dict).
    """
    from cryptography.hazmat.primitives import serialization as _ser

    _, public_pem = rsa_keypair
    pubkey = _ser.load_pem_public_key(public_pem.encode())
    numbers = pubkey.public_numbers()  # type: ignore[attr-defined]
    # The modulus bytes (no PEM wrapper) are what an attacker
    # would use as the HMAC secret — the raw key material,
    # which is unique and well-known to anyone with the
    # public key.
    hmac_secret = numbers.n.to_bytes(
        (numbers.n.bit_length() + 7) // 8, "big"
    )
    now = int(time.time())
    claims = {
        "iss": "https://acme.cloudflareaccess.com",
        "aud": "00000000000000000000000000000000",
        "sub": "attacker",
        "iat": now,
        "exp": now + 300,
    }
    import json as _json

    from jwt.api_jws import PyJWS

    token = PyJWS().encode(
        _json.dumps(claims, separators=(",", ":")).encode(),
        hmac_secret,
        algorithm="HS256",
        headers={"kid": "test-key-1"},
    )

    verifier = JWTVerifier(
        issuer="https://acme.cloudflareaccess.com",
        audience="00000000000000000000000000000000",
        jwks_url=jwks_server_url,
        # ``RS256`` is the default; explicit here for clarity.
        algorithms=["RS256"],
    )
    assert await verifier.verify_token(token) is None


@pytest.mark.asyncio
async def test_garbage_token_returns_none(
    verifier: JWTVerifier,
) -> None:
    """A non-JWT string in the bearer slot → ``None``."""
    assert await verifier.verify_token("not-a-jwt") is None


@pytest.mark.asyncio
async def test_empty_token_returns_none(
    verifier: JWTVerifier,
) -> None:
    """Empty bearer → ``None``. (The SDK's middleware also returns 401 directly.)"""
    assert await verifier.verify_token("") is None


# --- Custom required-claims -------------------------------------------


@pytest.mark.asyncio
async def test_extra_required_claim_enforced(
    jwks_server_url: str, rsa_keypair: tuple[str, str]
) -> None:
    """Operators that need ``email`` (Google-OIDC) extend ``required_claims``."""
    private_pem, _ = rsa_keypair
    verifier = JWTVerifier(
        issuer="https://acme.cloudflareaccess.com",
        audience="00000000000000000000000000000000",
        jwks_url=jwks_server_url,
        required_claims=("exp", "iat", "iss", "aud", "sub", "email"),
    )
    # Token without ``email`` → ``None``. ``_sign`` defaults to
    # including ``email``; passing ``email=None`` strips it
    # from the claim set so we test the "missing required
    # claim" rejection.
    token_no_email = _sign(private_pem, email=None)
    decoded = jwt.decode(
        token_no_email, options={"verify_signature": False}
    )
    assert "email" not in decoded
    assert await verifier.verify_token(token_no_email) is None

    # Token with ``email`` → accepted.
    token_with_email = _sign(private_pem, email="[email protected]")
    assert await verifier.verify_token(token_with_email) is not None


# --- select_verifier factory -------------------------------------------


def test_select_verifier_picks_jwt_in_jwt_mode() -> None:
    """All three JWT env vars present → :class:`JWTVerifier`."""
    v = select_verifier(
        auth_mode="jwt",
        static_token=None,
        jwt_issuer="https://acme.cloudflareaccess.com",
        jwt_audience="aud",
        jwt_jwks_url="https://acme.cloudflareaccess.com/certs",
    )
    assert isinstance(v, JWTVerifier)


def test_select_verifier_picks_static_in_static_mode() -> None:
    """``auth_mode=static`` + ``static_token`` → :class:`StaticTokenVerifier`."""
    v = select_verifier(
        auth_mode="static",
        static_token="secret",
        jwt_issuer=None,
        jwt_audience=None,
        jwt_jwks_url=None,
    )
    assert isinstance(v, StaticTokenVerifier)


@pytest.mark.parametrize(
    "missing",
    [
        {"jwt_issuer": None},
        {"jwt_audience": None},
        {"jwt_jwks_url": None},
        {"jwt_issuer": None, "jwt_audience": None},
        {"jwt_issuer": None, "jwt_jwks_url": None},
        {"jwt_audience": None, "jwt_jwks_url": None},
        {"jwt_issuer": None, "jwt_audience": None, "jwt_jwks_url": None},
    ],
)
def test_select_verifier_jwt_mode_requires_all_three(missing: dict) -> None:
    """Any of the three JWT env vars unset → ``ValueError``."""
    kwargs = {
        "auth_mode": "jwt",
        "static_token": None,
        "jwt_issuer": "https://acme.cloudflareaccess.com",
        "jwt_audience": "aud",
        "jwt_jwks_url": "https://acme.cloudflareaccess.com/certs",
    }
    kwargs.update(missing)
    with pytest.raises(ValueError, match="MCP_SILVERBULLET_JWT_"):
        select_verifier(**kwargs)


def test_select_verifier_static_mode_requires_token() -> None:
    """``auth_mode=static`` + no token → ``ValueError``."""
    with pytest.raises(ValueError, match="MCP_SILVERBULLET_TOKEN"):
        select_verifier(
            auth_mode="static",
            static_token="",
            jwt_issuer=None,
            jwt_audience=None,
            jwt_jwks_url=None,
        )


def test_select_verifier_unknown_mode_rejected() -> None:
    """``auth_mode="foo"`` → ``ValueError`` so a typo doesn't downgrade security."""
    with pytest.raises(ValueError, match="unknown MCP_SILVERBULLET_AUTH_MODE"):
        select_verifier(
            auth_mode="foo",
            static_token=None,
            jwt_issuer=None,
            jwt_audience=None,
            jwt_jwks_url=None,
        )
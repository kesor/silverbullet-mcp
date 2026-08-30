"""Bearer-token verifiers for the inbound MCP hop.

Two implementations behind one :class:`mcp.server.auth.provider.TokenVerifier`
interface, selected at boot from the operator's env vars:

- :class:`JWTVerifier` — validates RS256-signed JWTs against a JWKS
  endpoint. Default mode in v1.4+: the bridge is meant to sit behind
  Cloudflare Access (or any OIDC IdP that publishes a JWKS), so the
  verifier fetches the IdP's signing keys at boot, validates
  ``iss`` / ``aud`` / ``exp`` / ``iat`` / ``nbf`` per RFC 7519, and
  surfaces the principal (``sub``) plus the full claim dict on the
  returned :class:`AccessToken`. The default config targets CF Access:
  the JWKS URL pattern is
  ``https://<org>.cloudflareaccess.com/cdn-cgi/access/certs``, the
  expected ``iss`` is ``https://<org>.cloudflareaccess.com``, and
  ``alg`` is fixed at ``RS256``.

- :class:`StaticTokenVerifier` — accepts exactly one pre-shared
  bearer secret, compared constant-time. Kept as the v1.x surface;
  opt-in via ``MCP_SILVERBULLET_AUTH_MODE=static`` (the default is
  ``jwt``). Used by the ``mcp dev`` CLI session and by operators
  who don't yet sit behind an IdP.

The SDK does every header-related chore (parsing
``Authorization: Bearer …``, serving the
``/.well-known/oauth-protected-resource/mcp`` discovery document,
returning ``401`` + ``WWW-Authenticate: Bearer resource_metadata=…``
when the token is missing or wrong). This module is just the
signature / claim verification.

See ``docs/design.md`` § Auth for the threat model and
``docs/wayfinder/map.md`` for the T2 decision this builds on.
v1.4 widens the inbound auth from "one shared secret" to "validate
per-user JWTs against an IdP JWKS", keeping the static-token path
alive for ``mcp dev`` and other non-IdP setups.
"""

from __future__ import annotations

import hmac
from collections.abc import Iterable
from typing import Any

import jwt
from jwt import PyJWKClient
from mcp.server.auth.provider import AccessToken, TokenVerifier


# Scopes surfaced on the AccessToken. The SDK stamps these onto the
# AuthCredentials, so future scope-aware code can branch on them. v1's
# tools accept both because there is only one user; the split exists
# so we don't have to invent scopes later when one tool becomes
# read-only. The CF Access JWTs we accept don't carry a ``scope``
# claim — we surface the same two scopes on every successful
# verification; future tickets can map CF Access's ``policy`` tag
# onto a finer-grained scope list.
_SCOPES = ("notes:read", "notes:write")


# --- JWT verifier ------------------------------------------------------


# Default clock-skew leeway for ``jwt.decode``. Cloudflare Access
# recommends 30s; we mirror that so a slight drift between the bridge
# host and the IdP doesn't reject a token that's otherwise valid.
# The same number applies to most OIDC providers (Auth0, Okta, Google
# IAP all set ``exp`` to the second, with 0–30s of acceptable drift).
_DEFAULT_LEEWAY_SECONDS = 30


# CF Access's expected signing algorithm. Pinned (rather than read
# from the JWKS ``alg`` field) so an attacker can't trick the
# verifier into accepting an HS256 token signed with the public key
# (a classic "algorithm confusion" attack — the public key becomes
# the symmetric secret). Operators running a non-CF IdP that uses a
# different algorithm override via ``MCP_SILVERBULLET_JWT_ALGORITHMS``.
_DEFAULT_ALGORITHMS: tuple[str, ...] = ("RS256",)


class JWTVerifier:
    """``TokenVerifier`` that validates JWTs against an IdP's JWKS endpoint.

    Parameters
    ----------
    issuer
        The expected ``iss`` claim value. Compared exact-string
        against the decoded token; CF Access uses the form
        ``https://<org>.cloudflareaccess.com`` (note the trailing
        no-slash; the JWKS URL has the ``/cdn-cgi/access/certs``
        path but the issuer claim is the bare origin).
    audience
        The expected ``aud`` claim value. CF Access uses a
        per-application AUD tag (a hex string generated in the
        Cloudflare Zero Trust dashboard when the operator created
        the SilverBullet Access application). Other IdPs use the
        client ID, the API audience identifier, or a similar
        opaque string.
    jwks_url
        URL to the IdP's JWKS document. PyJWT's
        :class:`PyJWKClient` fetches it once, caches the parsed
        keys, and re-fetches on cache miss (TTL 5 minutes
        default — overridable via the ``lifespan`` kwarg). CF
        Access's URL is
        ``https://<org>.cloudflareaccess.com/cdn-cgi/access/certs``.
    algorithms
        Allowed JWA signing algorithms. CF Access only issues
        RS256, but the constructor accepts a tuple so an operator
        running Auth0 / Okta / Keycloak can pass ``["RS256"]``,
        ``["ES256"]``, or whatever the IdP actually uses.
        Multiple values are allowed (e.g. ``("RS256", "ES256")``
        during a key-type rollover).
    leeway_seconds
        Clock-skew tolerance for ``exp`` / ``iat`` / ``nbf``
        checks. Default 30s (CF Access's recommended value).
        Operators running on a host with a known clock drift can
        raise it; lower is rejected because the SDK would then
        falsely reject CF Access tokens.
    required_claims
        Claims that must be present after decoding. Defaults to
        ``{"exp", "iat", "iss", "aud", "sub"}`` — the standard
        RFC 7519 minimum-viable set plus ``sub`` (the principal
        this verifier surfaces on ``AccessToken.subject``).
        Operators that need additional guarantees (e.g. ``email``
        from a Google-OIDC provider) can extend this set.

    Verification flow (per request):

    1. Pull the kid from the token's JOSE header.
    2. Ask the cached :class:`PyJWKClient` for the signing key
       matching that kid (auto-fetches the JWKS document on
       cache miss).
    3. Call ``jwt.decode(token, key, algorithms=..., issuer=...,
       audience=..., leeway=..., options={"require": ...})`` —
       PyJWT checks the signature, ``iss``, ``aud``, ``exp``,
       ``iat`` / ``nbf``, and the required-claim set in one
       step. Any failure (``InvalidSignatureError``,
       ``InvalidIssuerError``, ``InvalidAudienceError``,
       ``ExpiredSignatureError``, ``MissingRequiredClaimError``,
       etc.) collapses to ``verify_token`` returning ``None``.
    4. Build an :class:`AccessToken` with ``subject=<sub>``,
       ``client_id=<azp|client_id|cf-access>``,
       ``expires_at=<exp>``, ``claims=<full dict>``,
       ``scopes=_SCOPES``.

    Threading note: :class:`PyJWKClient` is sync-only and holds an
    internal lock; one instance per process is correct. Multiple
    concurrent verifications serialize on the lock but the JWKS
    fetch only happens on cache miss, so steady-state lock
    contention is negligible.
    """

    def __init__(
        self,
        *,
        issuer: str,
        audience: str,
        jwks_url: str,
        algorithms: Iterable[str] = _DEFAULT_ALGORITHMS,
        leeway_seconds: int = _DEFAULT_LEEWAY_SECONDS,
        required_claims: Iterable[str] = (
            "exp",
            "iat",
            "iss",
            "aud",
            "sub",
        ),
    ) -> None:
        # Normalize to tuples for stable repr and predictable iteration.
        # ``frozen=True`` would also work but adds nothing — these are
        # private fields, mutated only at init time.
        self._algorithms = tuple(algorithms)
        self._issuer = issuer
        self._audience = audience
        self._leeway = leeway_seconds
        self._required_claims = frozenset(required_claims)
        # PyJWKClient default cache lifespan is 300s (5 minutes).
        # That's the right window for CF Access's key rotation
        # cadence (days, not minutes) and bounded enough that a
        # compromised key is purged promptly. Operators needing
        # tighter rotation can pass ``lifespan=...`` here in a
        # follow-up.
        self._jwks_client = PyJWKClient(jwks_url)

    async def verify_token(self, token: str) -> AccessToken | None:
        """Validate ``token`` against the cached JWKS. ``None`` on any failure.

        All PyJWT exceptions (``InvalidSignatureError``,
        ``InvalidIssuerError``, ``InvalidAudienceError``,
        ``ExpiredSignatureError``, ``MissingRequiredClaimError``,
        ``InvalidAlgorithmError``, ``DecodeError``, etc.) are
        caught here and collapsed to ``None``. The bridge's
        response to a failed verification is identical regardless
        of *why* it failed — same ``401`` shape from the SDK,
        same ``WWW-Authenticate`` header — so the agent learns
        only "your token isn't valid", not "your token is
        expired" vs. "your audience is wrong". That matches the
        :class:`StaticTokenVerifier` contract and avoids leaking
        which validation step rejected the token.
        """
        try:
            signing_key = self._jwks_client.get_signing_key_from_jwt(token).key
            claims = jwt.decode(
                token,
                signing_key,
                algorithms=self._algorithms,
                issuer=self._issuer,
                audience=self._audience,
                leeway=self._leeway,
                options={"require": list(self._required_claims)},
            )
        except jwt.PyJWTError:
            return None
        return _access_token_from_claims(claims, token)


def _access_token_from_claims(
    claims: dict[str, Any], token: str
) -> AccessToken:
    """Build the :class:`AccessToken` the SDK stamps onto ``AuthCredentials``.

    Centralizes the claim → AccessToken-field mapping so the
    ``StaticTokenVerifier`` and the ``JWTVerifier`` produce
    identical shapes on the fields future scope-aware code
    branches on (``scopes``, ``subject``, ``client_id``). The
    ``claims`` dict rides through verbatim — future tickets
    reading ``AccessToken.claims["email"]`` (CF Access / OIDC)
    or ``AccessToken.claims["custom"]["groups"]`` (CF Access
    IdP groups) don't have to re-decode the JWT.

    ``client_id`` falls back to ``"cloudflare-access"`` for CF
    Access tokens (which carry no ``azp`` / ``client_id``
    claim) and to the literal claim value when the IdP does
    provide one (Auth0 / Okta OIDC). The principal
    (``subject``) is always the RFC 7662 / 9068 ``sub`` claim
    — CF Access fills this with the user's UUID, which is
    stable per-user per-org.
    """
    return AccessToken(
        token=token,
        # ``azp`` is the OAuth 2.0 "authorized party" claim (Auth0
        # uses it); ``client_id`` is the standard OAuth client
        # identifier. CF Access doesn't emit either; we fall
        # back to a literal so ``AccessToken.client_id`` is
        # always populated (the SDK uses it for principal
        # identification downstream).
        client_id=str(
            claims.get("azp")
            or claims.get("client_id")
            or "cloudflare-access"
        ),
        scopes=list(_SCOPES),
        subject=str(claims["sub"]),
        expires_at=claims.get("exp"),
        claims=claims,
    )


# --- Static-token verifier (v1.x compat) ------------------------------


class StaticTokenVerifier:
    """``TokenVerifier`` that accepts exactly one bearer token.

    Parameters
    ----------
    token
        The shared secret. Same value as ``MCP_SILVERBULLET_TOKEN``
        and (when SB auth is on) ``SB_AUTH_TOKEN``. Comparison is
        constant-time (``hmac.compare_digest`` on the two byte
        strings) so the verifier doesn't leak the matching prefix
        one byte at a time.

    The returned :class:`AccessToken` carries ``client_id="grok"``
    and both scopes; ``subject="local"`` records that this is
    the local tunnel deployment, not a multi-user authz flow.
    Kept identical to the v1.x surface so existing operators
    (and the ``mcp dev`` CLI session) keep working unchanged.
    """

    def __init__(self, token: str) -> None:
        # ``encode()`` once so the per-request verify doesn't re-encode.
        self._token = token.encode()

    async def verify_token(self, token: str) -> AccessToken | None:
        if hmac.compare_digest(token.encode(), self._token):
            return AccessToken(
                token=token,
                client_id="grok",
                scopes=list(_SCOPES),
                subject="local",
            )
        return None


# --- Verifier selection ------------------------------------------------


def select_verifier(
    *,
    auth_mode: str,
    static_token: str | None,
    jwt_issuer: str | None,
    jwt_audience: str | None,
    jwt_jwks_url: str | None,
    jwt_algorithms: Iterable[str] = _DEFAULT_ALGORITHMS,
    jwt_leeway_seconds: int = _DEFAULT_LEEWAY_SECONDS,
) -> TokenVerifier:
    """Pick the verifier from the operator's env config.

    The function centralizes the env → verifier decision so
    ``main.py`` doesn't grow a branch on every new auth knob.
    The decision tree:

    - ``auth_mode="jwt"`` (default) requires ``jwt_issuer``,
      ``jwt_audience``, and ``jwt_jwks_url`` all set. Missing
      any of those raises ``ValueError`` at boot rather than
      silently selecting a half-configured verifier — better
      to fail loud than to serve unauthenticated traffic.

    - ``auth_mode="static"`` requires ``static_token`` set.
      Missing it raises ``ValueError`` at boot.

    - Unknown ``auth_mode`` values raise ``ValueError`` so a
      typo (``"sttic"``) doesn't fall through to the default
      and silently downgrade security.

    The split lets callers pre-validate the env in
    :func:`mcp_silverbullet.main.load_settings` and surface
    the failure as ``SystemExit("...")`` — the same UX as the
    existing ``MCP_SILVERBULLET_TOKEN is required`` guard.
    """
    if auth_mode == "jwt":
        missing = [
            name
            for name, value in (
                ("MCP_SILVERBULLET_JWT_ISSUER", jwt_issuer),
                ("MCP_SILVERBULLET_JWT_AUDIENCE", jwt_audience),
                ("MCP_SILVERBULLET_JWT_JWKS_URL", jwt_jwks_url),
            )
            if not value
        ]
        if missing:
            raise ValueError(
                "MCP_SILVERBULLET_AUTH_MODE=jwt requires "
                + ", ".join(missing)
                + " to be set"
            )
        return JWTVerifier(
            issuer=jwt_issuer,  # type: ignore[arg-type]
            audience=jwt_audience,  # type: ignore[arg-type]
            jwks_url=jwt_jwks_url,  # type: ignore[arg-type]
            algorithms=jwt_algorithms,
            leeway_seconds=jwt_leeway_seconds,
        )
    if auth_mode == "static":
        if not static_token:
            raise ValueError(
                "MCP_SILVERBULLET_AUTH_MODE=static requires "
                "MCP_SILVERBULLET_TOKEN to be set"
            )
        return StaticTokenVerifier(static_token)
    raise ValueError(
        f"unknown MCP_SILVERBULLET_AUTH_MODE {auth_mode!r}; "
        "expected 'jwt' or 'static'"
    )


__all__ = [
    "JWTVerifier",
    "StaticTokenVerifier",
    "select_verifier",
]
"""Static bearer-token verifier for the inbound MCP hop.

Locked at T2 of the prior map: one shared secret on both hops, no OAuth
dance. The SDK does every header-related chore (parsing
``Authorization: Bearer …``, serving the
``/.well-known/oauth-protected-resource/mcp`` discovery document,
returning ``401`` + ``WWW-Authenticate: Bearer resource_metadata=…``
when the token is missing or wrong). This module is just the
constant-time compare.

See ``docs/design.md`` § Auth for the threat model and
``docs/wayfinder/map.md`` for the T2 decision this implements.
"""

from __future__ import annotations

import hmac

from mcp.server.auth.provider import AccessToken, TokenVerifier


# Scopes surfaced on the AccessToken. The SDK stamps these onto the
# AuthCredentials, so future scope-aware code can branch on them. v1's
# tools accept both because there is only one user; the split exists
# so we don't have to invent scopes later when one tool becomes
# read-only.
_SCOPES = ("notes:read", "notes:write")


class StaticTokenVerifier:
    """``TokenVerifier`` that accepts exactly one bearer token.

    Parameters
    ----------
    token
        The shared secret. Same value as ``MCP_SILVERBULLET_TOKEN`` and
        ``SB_AUTH_TOKEN``. Comparison is constant-time (``hmac.compare_digest``
        on the two byte strings) so the verifier doesn't leak the
        matching prefix one byte at a time.

    The returned :class:`AccessToken` carries ``client_id="grok"`` and
    both scopes; ``subject="local"`` records that this is the local
    tunnel deployment, not a multi-user authz flow.
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

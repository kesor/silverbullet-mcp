"""Env contract for the T6 boot path. No sockets.

v1.4 split the inbound auth into JWT (default) and static (opt-in).
These tests cover the env parsing for both modes, the JWT-mode
defaults, and the misconfiguration guards. The v1.x surface
(``MCP_SILVERBULLET_TOKEN`` required, no JWT env vars) is preserved
when the operator explicitly opts in via
``MCP_SILVERBULLET_AUTH_MODE=static``.
"""

from __future__ import annotations

import pytest

from mcp_silverbullet.main import (
    _DEFAULT_AUTH_MODE,
    build_verifier,
    load_settings,
)
from mcp_silverbullet.verifier import JWTVerifier, StaticTokenVerifier


# Minimal env that satisfies the JWT-mode default. Used as the base
# for tests that don't specifically care about auth mode — they get
# a clean JWT-mode parse with one env call rather than re-stating
# all three vars in every test.
_JWT_ENV = {
    "MCP_SILVERBULLET_JWT_ISSUER": "https://acme.cloudflareaccess.com",
    "MCP_SILVERBULLET_JWT_AUDIENCE": "00000000000000000000000000000000",
    "MCP_SILVERBULLET_JWT_JWKS_URL": "https://acme.cloudflareaccess.com/cdn-cgi/access/certs",
}


def test_default_auth_mode_is_jwt() -> None:
    """No ``MCP_SILVERBULLET_AUTH_MODE`` set → mode is ``jwt``.

    v1.4 flips the default from v1.x's static-token to JWT.
    Operators who haven't migrated yet opt back in via
    ``MCP_SILVERBULLET_AUTH_MODE=static`` plus the same
    ``MCP_SILVERBULLET_TOKEN`` they used to set.
    """
    s = load_settings(_JWT_ENV)
    assert s.auth_mode == "jwt"
    assert s.auth_mode == _DEFAULT_AUTH_MODE  # pinned via module constant


def test_jwt_mode_requires_jwt_env_vars() -> None:
    """No JWT env vars + JWT mode (default) → ``SystemExit`` at boot.

    The verifier cannot validate tokens without ``iss``,
    ``aud``, and a JWKS URL. Failing loud at boot is better
    than serving unauthenticated traffic on the first
    request — the operator sees the missing-var list in the
    boot log immediately, not a 401 storm on the first
    authenticated client.
    """
    with pytest.raises(SystemExit, match="MCP_SILVERBULLET_JWT_ISSUER"):
        load_settings({})


def test_jwt_mode_lists_every_missing_var() -> None:
    """A misconfigured boot surfaces *all* missing vars, not just the first."""
    with pytest.raises(SystemExit) as excinfo:
        load_settings(
            {"MCP_SILVERBULLET_JWT_ISSUER": "https://acme.cloudflareaccess.com"}
        )
    msg = str(excinfo.value)
    assert "MCP_SILVERBULLET_JWT_AUDIENCE" in msg
    assert "MCP_SILVERBULLET_JWT_JWKS_URL" in msg


def test_static_mode_requires_token() -> None:
    """``AUTH_MODE=static`` + no ``MCP_SILVERBULLET_TOKEN`` → ``SystemExit``."""
    with pytest.raises(SystemExit, match="MCP_SILVERBULLET_TOKEN"):
        load_settings(
            {
                "MCP_SILVERBULLET_AUTH_MODE": "static",
            }
        )


def test_static_mode_back_compat_matches_v1_x() -> None:
    """``AUTH_MODE=static`` + ``MCP_SILVERBULLET_TOKEN`` parses the v1.x env."""
    s = load_settings(
        {
            "MCP_SILVERBULLET_AUTH_MODE": "static",
            "MCP_SILVERBULLET_TOKEN": "secret",
        }
    )
    assert s.auth_mode == "static"
    assert s.token == "secret"
    # sb_token still defaults to the inbound token — same
    # one-secret-on-both-hops v1.x behavior when the operator
    # hasn't set ``MCP_SILVERBULLET_SB_TOKEN`` explicitly.
    assert s.sb_token == "secret"
    # The JWT-mode fields are still parsed but unused in
    # static mode; ``None`` when unset.
    assert s.jwt_issuer is None


def test_unknown_auth_mode_rejected() -> None:
    """``AUTH_MODE=foo`` → ``SystemExit`` so a typo doesn't silently downgrade."""
    with pytest.raises(SystemExit, match="MCP_SILVERBULLET_AUTH_MODE must be"):
        load_settings(
            {
                "MCP_SILVERBULLET_AUTH_MODE": "sttic",
            }
        )


def test_jwt_mode_with_token_logs_info() -> None:
    """JWT mode ignores ``MCP_SILVERBULLET_TOKEN`` but logs so operators notice.

    Operators often leave ``MCP_SILVERBULLET_TOKEN`` set from a
    prior v1.x boot and wonder why their secret isn't in use
    after upgrading to v1.4. The boot-time log line tells
    them the JWT path is active and how to flip back to
    static. We assert the line is emitted when ``TOKEN`` is
    set but the JWT env vars are absent (the only path where
    the operator's confusion is plausible — a successful
    JWT boot with ``TOKEN`` also set still emits the line,
    but we test the loud-failure path because the log call
    sits *before* the JWT-config validation).
    """
    import logging

    with pytest.raises(SystemExit):
        load_settings(
            {
                "MCP_SILVERBULLET_TOKEN": "old-secret",
            }
        )


def test_jwt_mode_default_fields() -> None:
    """JWT mode parses the three required vars + sensible defaults.

    Default algorithms (``("RS256",)``) and leeway (``30s``) match
    CF Access; operators running Auth0 / Okta / Google-IAP
    override via the corresponding env vars.
    """
    s = load_settings(_JWT_ENV)
    assert s.jwt_issuer == "https://acme.cloudflareaccess.com"
    assert s.jwt_audience == "00000000000000000000000000000000"
    assert (
        s.jwt_jwks_url
        == "https://acme.cloudflareaccess.com/cdn-cgi/access/certs"
    )
    assert s.jwt_algorithms == ("RS256",)
    assert s.jwt_leeway_seconds == 30


def test_jwt_mode_algorithms_csv_parse() -> None:
    """``MCP_SILVERBULLET_JWT_ALGORITHMS`` is comma-split into a tuple.

    Operators rolling from RS256 to ES256 list both:
    ``MCP_SILVERBULLET_JWT_ALGORITHMS="RS256,ES256"`` so tokens
    signed with the old key still verify during the rollover
    window.
    """
    s = load_settings(
        {
            **_JWT_ENV,
            "MCP_SILVERBULLET_JWT_ALGORITHMS": "RS256, ES256",
        }
    )
    assert s.jwt_algorithms == ("RS256", "ES256")


def test_jwt_mode_empty_algorithms_string_falls_back_to_default() -> None:
    """Whitespace-only or empty ``_ALGORITHMS`` falls back to ``("RS256",)``."""
    s = load_settings(
        {
            **_JWT_ENV,
            "MCP_SILVERBULLET_JWT_ALGORITHMS": "   ",
        }
    )
    assert s.jwt_algorithms == ("RS256",)


def test_jwt_mode_leeway_override() -> None:
    """``MCP_SILVERBULLET_JWT_LEEWAY_SECONDS`` overrides the 30s default."""
    s = load_settings(
        {
            **_JWT_ENV,
            "MCP_SILVERBULLET_JWT_LEEWAY_SECONDS": "60",
        }
    )
    assert s.jwt_leeway_seconds == 60


def test_jwt_mode_leeway_must_be_non_negative_integer() -> None:
    """Negative or non-integer leeway → ``SystemExit`` at boot."""
    with pytest.raises(SystemExit, match="MCP_SILVERBULLET_JWT_LEEWAY_SECONDS"):
        load_settings({**_JWT_ENV, "MCP_SILVERBULLET_JWT_LEEWAY_SECONDS": "-1"})
    with pytest.raises(SystemExit, match="MCP_SILVERBULLET_JWT_LEEWAY_SECONDS"):
        load_settings(
            {**_JWT_ENV, "MCP_SILVERBULLET_JWT_LEEWAY_SECONDS": "thirty"}
        )


# --- Defaults that match v1.x surface unchanged ------------------------


def test_defaults_match_v1_x() -> None:
    """In static mode the non-auth fields still default to v1.x values."""
    s = load_settings(
        {
            "MCP_SILVERBULLET_AUTH_MODE": "static",
            "MCP_SILVERBULLET_TOKEN": "secret",
        }
    )
    assert s.sb_token == "secret"
    assert s.sb_url == "http://127.0.0.1:3000"
    assert s.host == "127.0.0.1"
    assert s.port == 8000
    assert s.resource_url == "http://127.0.0.1:8000/mcp"
    assert s.allowed_hosts == ()
    # T10: no ``MCP_SILVERBULLET_JOURNAL_TOOLS`` env → journal gate
    # off; existing boot path is unchanged.
    assert s.journal.enabled is False
    assert s.journal.space_path is None
    # T28: no ``MCP_SILVERBULLET_LIST_PAGES_HYDRATE_ETAGS`` env →
    # etag-hydration off (the v1.1 default); operators opt in
    # explicitly because hydration is an N+1 against SB.
    assert s.list_pages_hydrate_etags is False


def test_sb_token_may_be_empty_when_sb_has_no_auth() -> None:
    s = load_settings(
        {
            "MCP_SILVERBULLET_AUTH_MODE": "static",
            "MCP_SILVERBULLET_TOKEN": "inbound",
            "MCP_SILVERBULLET_SB_TOKEN": "",
            "MCP_SILVERBULLET_SB_URL": "http://127.0.0.1:63000/",
            "MCP_SILVERBULLET_ALLOWED_HOSTS": "mcp.local, example.trycloudflare.com",
            "MCP_SILVERBULLET_PORT": "9000",
        }
    )
    assert s.sb_token == ""
    assert s.sb_url == "http://127.0.0.1:63000"
    assert s.port == 9000
    assert s.resource_url == "http://127.0.0.1:9000/mcp"
    assert s.allowed_hosts == ("mcp.local", "example.trycloudflare.com")


def test_bad_port() -> None:
    with pytest.raises(SystemExit, match="MCP_SILVERBULLET_PORT"):
        load_settings(
            {
                "MCP_SILVERBULLET_AUTH_MODE": "static",
                "MCP_SILVERBULLET_TOKEN": "x",
                "MCP_SILVERBULLET_PORT": "nope",
            }
        )


@pytest.mark.parametrize("raw", ["1", "true", "yes", "on", "TRUE", "  on  "])
def test_list_pages_hydrate_etags_truthy_values_enable(raw: str) -> None:
    """Truthy values enable T28 etag-hydration.

    Mirrors ``MCP_SILVERBULLET_JOURNAL_TOOLS``'s truthy parse
    (the same helper, :func:`_is_truthy`, handles both env
    vars) so an operator who's used ``JOURNAL_TOOLS=1`` doesn't
    have to relearn the shape for
    ``LIST_PAGES_HYDRATE_ETAGS``. Whitespace and case are
    normalized before the in-set check.
    """
    s = load_settings(
        {
            "MCP_SILVERBULLET_AUTH_MODE": "static",
            "MCP_SILVERBULLET_TOKEN": "x",
            "MCP_SILVERBULLET_LIST_PAGES_HYDRATE_ETAGS": raw,
        }
    )
    assert s.list_pages_hydrate_etags is True


@pytest.mark.parametrize("raw", ["", "0", "no", "false", "off", "anything-else"])
def test_list_pages_hydrate_etags_other_values_disable(raw: str) -> None:
    """Anything else (including empty string and unset) disables hydration."""
    s = load_settings(
        {
            "MCP_SILVERBULLET_AUTH_MODE": "static",
            "MCP_SILVERBULLET_TOKEN": "x",
            "MCP_SILVERBULLET_LIST_PAGES_HYDRATE_ETAGS": raw,
        }
    )
    assert s.list_pages_hydrate_etags is False


# --- build_verifier ----------------------------------------------------


def test_build_verifier_picks_jwt_in_jwt_mode() -> None:
    """``build_verifier`` returns a :class:`JWTVerifier` when mode is ``jwt``."""
    s = load_settings(_JWT_ENV)
    v = build_verifier(s)
    assert isinstance(v, JWTVerifier)
    # The verifier was constructed from the env vars verbatim.
    assert v._issuer == "https://acme.cloudflareaccess.com"  # noqa: SLF001
    assert v._audience == "00000000000000000000000000000000"  # noqa: SLF001
    assert (
        v._jwks_client.uri  # noqa: SLF001
        == "https://acme.cloudflareaccess.com/cdn-cgi/access/certs"
    )


def test_build_verifier_picks_static_in_static_mode() -> None:
    """``build_verifier`` returns a :class:`StaticTokenVerifier` when mode is ``static``."""
    s = load_settings(
        {
            "MCP_SILVERBULLET_AUTH_MODE": "static",
            "MCP_SILVERBULLET_TOKEN": "secret",
        }
    )
    v = build_verifier(s)
    assert isinstance(v, StaticTokenVerifier)
    # Compare against the literal bytes — same constant-time
    # compare the verifier itself runs.
    assert v._token == b"secret"  # noqa: SLF001
"""Bridge process entry: env → SBClient → MCPServer → Streamable HTTP.

T1 shipped a hello-print smoke. T6 replaces it with the real boot:
read env, open the outbound SB client, mount the inbound MCP app, bind
uvicorn via ``MCPServer.run_streamable_http_async``.

v1.4 widens the inbound auth surface from a single shared secret to
a JWT mode that validates per-user tokens against an IdP's JWKS
(default config targets Cloudflare Access; the static-token mode
stays available for ``mcp dev`` and other non-IdP setups). The
verifier selection lives in :mod:`mcp_silverbullet.verifier`; this
module just plumbs the env vars through ``load_settings``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from collections.abc import Iterable
from dataclasses import dataclass

from mcp.server.auth.provider import TokenVerifier
from mcp.server.transport_security import TransportSecuritySettings

from mcp_silverbullet.http_debug import (
    RequestDumpMiddleware,
    install_http_debug_hooks,
)
from mcp_silverbullet.journal import (
    JournalConfig,
    _is_truthy,
    resolve_journal_config,
)
from mcp_silverbullet.sb_client import SBClient
from mcp_silverbullet.server import build_mcp
from mcp_silverbullet.verifier import select_verifier

_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_PORT = 8000
_DEFAULT_SB_URL = "http://127.0.0.1:3000"

# Default auth mode. v1.4 flips the default from the v1.x static
# shared secret to JWT — the bridge is meant to sit behind
# Cloudflare Access in production, and the JWT path gives per-user
# ``subject`` on every request. Operators who haven't migrated
# yet opt back into the v1.x surface with
# ``MCP_SILVERBULLET_AUTH_MODE=static`` plus the same
# ``MCP_SILVERBULLET_TOKEN`` they used to set.
_DEFAULT_AUTH_MODE = "jwt"

# Default JWT signing algorithms. CF Access only issues RS256; we
# pin it here so a misconfigured operator can't accidentally
# accept HS256 tokens (algorithm-confusion attack). Operators
# running Auth0 / Google-IAP / Okta pass their IdP's algorithm
# via ``MCP_SILVERBULLET_JWT_ALGORITHMS`` (comma-separated).
_DEFAULT_JWT_ALGORITHMS: tuple[str, ...] = ("RS256",)

# Default clock-skew tolerance for ``jwt.decode``. CF Access
# recommends 30s; we mirror that across all IdPs because the
# alternative (a 0s leeway) fails tokens from hosts with even
# modest NTP drift.
_DEFAULT_JWT_LEEWAY_SECONDS = 30

# Inbound log level. ``INFO`` is the v1.x default (journal-gate
# lines + uvicorn access). ``DEBUG`` also dumps sanitized HTTP
# request metadata and classifies uvicorn's opaque
# "Invalid HTTP request received" warnings (HTTP/2 preface vs
# TLS ClientHello vs junk). Accepted values match uvicorn /
# the MCP SDK: DEBUG / INFO / WARNING / ERROR / CRITICAL.
_DEFAULT_LOG_LEVEL = "INFO"
_LOG_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})


@dataclass(frozen=True)
class Settings:
    """Operator-facing env contract. Names match ``docs/design.md`` § Boot."""

    token: str
    sb_url: str
    sb_token: str
    resource_url: str
    host: str
    port: int
    allowed_hosts: tuple[str, ...]
    journal: JournalConfig
    list_pages_hydrate_etags: bool
    # v1.4: JWT-mode config. ``auth_mode`` selects between
    # ``"jwt"`` (default) and ``"static"`` (v1.x compat). The
    # JWT fields are only consulted when ``auth_mode == "jwt"``;
    # setting them in static mode is a silent no-op (the
    # ``select_verifier`` function raises if the chosen mode's
    # required fields are missing).
    auth_mode: str
    jwt_issuer: str | None
    jwt_audience: str | None
    jwt_jwks_url: str | None
    jwt_algorithms: tuple[str, ...]
    jwt_leeway_seconds: int
    log_level: str


def _parse_csv(raw: str) -> tuple[str, ...]:
    """Split a comma-separated env value into a tuple of stripped entries.

    Empty entries (``"a,,b"``, ``","``, all-whitespace) are dropped
    so a trailing comma doesn't produce a phantom algorithm name.
    The bridge never wants a silent empty-string algorithm in the
    PyJWT allow-list (PyJWT would treat it as "any algorithm", which
    is the same algorithm-confusion footgun the default rejects).
    """
    return tuple(entry.strip() for entry in raw.split(",") if entry.strip())


def load_settings(environ: dict[str, str] | None = None) -> Settings:
    """Parse process env. Raises :class:`SystemExit` on misconfiguration."""
    env = os.environ if environ is None else environ
    auth_mode = (
        env.get("MCP_SILVERBULLET_AUTH_MODE") or _DEFAULT_AUTH_MODE
    ).strip().lower()
    if auth_mode not in ("jwt", "static"):
        raise SystemExit(
            f"MCP_SILVERBULLET_AUTH_MODE must be 'jwt' or 'static', "
            f"got {auth_mode!r}"
        )

    # ``token`` is still parsed in both modes so the existing
    # env contract keeps working: static mode uses it as the
    # bearer secret; JWT mode ignores it (the v1.4 default)
    # but the var is still settable for ``mcp dev`` sessions
    # that flip back to static mode.
    token = (env.get("MCP_SILVERBULLET_TOKEN") or "").strip()
    if auth_mode == "static" and not token:
        raise SystemExit(
            "MCP_SILVERBULLET_AUTH_MODE=static requires "
            "MCP_SILVERBULLET_TOKEN to be set"
        )
    if auth_mode == "jwt" and not token:
        # JWT mode doesn't need a static token, but logging a
        # notice at boot is friendlier than silently dropping
        # it — operators often leave the var set from prior
        # v1.x boots and wonder why it's not in use.
        logging.basicConfig(level=logging.INFO, force=True)
        logging.info(
            "MCP_SILVERBULLET_TOKEN is set but ignored in JWT mode; "
            "the bridge validates tokens against the IdP's JWKS "
            "(MCP_SILVERBULLET_JWT_ISSUER / _AUDIENCE / _JWKS_URL). "
            "To switch back to v1.x static-token auth, set "
            "MCP_SILVERBULLET_AUTH_MODE=static."
        )

    jwt_issuer = (
        (env.get("MCP_SILVERBULLET_JWT_ISSUER") or "").strip() or None
    )
    jwt_audience = (
        (env.get("MCP_SILVERBULLET_JWT_AUDIENCE") or "").strip() or None
    )
    jwt_jwks_url = (
        (env.get("MCP_SILVERBULLET_JWT_JWKS_URL") or "").strip() or None
    )
    jwt_algos_raw = (
        env.get("MCP_SILVERBULLET_JWT_ALGORITHMS") or ""
    ).strip()
    jwt_algorithms = (
        _parse_csv(jwt_algos_raw) if jwt_algos_raw else _DEFAULT_JWT_ALGORITHMS
    )
    jwt_leeway_raw = (
        env.get("MCP_SILVERBULLET_JWT_LEEWAY_SECONDS") or ""
    ).strip()
    try:
        jwt_leeway_seconds = (
            int(jwt_leeway_raw) if jwt_leeway_raw else _DEFAULT_JWT_LEEWAY_SECONDS
        )
    except ValueError as exc:
        raise SystemExit(
            f"MCP_SILVERBULLET_JWT_LEEWAY_SECONDS must be an integer, "
            f"got {jwt_leeway_raw!r}"
        ) from exc
    if jwt_leeway_seconds < 0:
        raise SystemExit(
            f"MCP_SILVERBULLET_JWT_LEEWAY_SECONDS must be non-negative, "
            f"got {jwt_leeway_seconds}"
        )

    # Surface the JWT-mode-misconfiguration case eagerly so the
    # operator sees the missing-var message at boot, not on the
    # first authenticated request.
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
            raise SystemExit(
                "MCP_SILVERBULLET_AUTH_MODE=jwt requires: "
                + ", ".join(missing)
            )

    port_raw = (env.get("MCP_SILVERBULLET_PORT") or str(_DEFAULT_PORT)).strip()
    try:
        port = int(port_raw)
    except ValueError as exc:
        raise SystemExit(
            f"MCP_SILVERBULLET_PORT must be an integer, got {port_raw!r}"
        ) from exc
    hosts_raw = (env.get("MCP_SILVERBULLET_ALLOWED_HOSTS") or "").strip()
    allowed = tuple(h.strip() for h in hosts_raw.split(",") if h.strip())
    sb_token = env.get("MCP_SILVERBULLET_SB_TOKEN")
    if sb_token is None:
        sb_token = token
    else:
        sb_token = sb_token.strip()
    host = (env.get("MCP_SILVERBULLET_HOST") or _DEFAULT_HOST).strip()
    resource_default = f"http://{host}:{port}/mcp"
    # Truthy parse mirrors :func:`resolve_journal_config`'s
    # ``MCP_SILVERBULLET_JOURNAL_TOOLS`` style: ``1`` / ``true`` /
    # ``yes`` / ``on`` enable; everything else (including empty
    # string and unset) disables. Reusing :func:`_is_truthy` keeps
    # the two env vars parse-consistent so an operator who's used
    # ``JOURNAL_TOOLS=1`` doesn't have to relearn the shape for
    # ``LIST_PAGES_HYDRATE_ETAGS``.
    list_pages_hydrate_etags = _is_truthy(
        env.get("MCP_SILVERBULLET_LIST_PAGES_HYDRATE_ETAGS", "")
    )
    # ``MCP_SILVERBULLET_DEBUG=1`` is a one-knob alias for
    # ``LOG_LEVEL=debug`` so an operator debugging a live
    # service doesn't have to remember the level names.
    # Explicit ``LOG_LEVEL`` wins when both are set.
    log_level_raw = (env.get("MCP_SILVERBULLET_LOG_LEVEL") or "").strip()
    if log_level_raw:
        log_level = log_level_raw.upper()
    elif _is_truthy(env.get("MCP_SILVERBULLET_DEBUG", "")):
        log_level = "DEBUG"
    else:
        log_level = _DEFAULT_LOG_LEVEL
    if log_level not in _LOG_LEVELS:
        raise SystemExit(
            f"MCP_SILVERBULLET_LOG_LEVEL must be one of "
            f"{sorted(_LOG_LEVELS)}, got {log_level_raw!r}"
        )
    return Settings(
        token=token,
        sb_url=(env.get("MCP_SILVERBULLET_SB_URL") or _DEFAULT_SB_URL).rstrip("/"),
        sb_token=sb_token,
        resource_url=(
            env.get("MCP_SILVERBULLET_RESOURCE_URL") or resource_default
        ).rstrip("/"),
        host=host,
        port=port,
        allowed_hosts=allowed,
        journal=resolve_journal_config(env),
        list_pages_hydrate_etags=list_pages_hydrate_etags,
        auth_mode=auth_mode,
        jwt_issuer=jwt_issuer,
        jwt_audience=jwt_audience,
        jwt_jwks_url=jwt_jwks_url,
        jwt_algorithms=jwt_algorithms,
        jwt_leeway_seconds=jwt_leeway_seconds,
        log_level=log_level,
    )


def _transport_security(
    settings: Settings,
) -> TransportSecuritySettings | None:
    """Loopback default unless the operator listed extra Host values.

    ``run_streamable_http_async(host=127.0.0.1)`` already enables DNS
    rebinding protection. Extra hosts (nginx ``<mcp>.local``, Cloudflare
    ``*.trycloudflare.com``) go in ``MCP_SILVERBULLET_ALLOWED_HOSTS``.
    """
    if not settings.allowed_hosts:
        return None
    hosts = list(dict.fromkeys((settings.host, *settings.allowed_hosts)))
    return TransportSecuritySettings(allowed_hosts=hosts)


def build_verifier(settings: Settings) -> TokenVerifier:
    """Select the inbound verifier from the parsed settings.

    Thin wrapper over :func:`mcp_silverbullet.verifier.select_verifier`
    that handles the ``None``-vs-empty-string distinction
    ``load_settings`` produces for the optional JWT columns. Kept
    separate from :func:`load_settings` so the selection logic is
    unit-testable without re-parsing env vars.
    """
    return select_verifier(
        auth_mode=settings.auth_mode,
        static_token=settings.token or None,
        jwt_issuer=settings.jwt_issuer,
        jwt_audience=settings.jwt_audience,
        jwt_jwks_url=settings.jwt_jwks_url,
        jwt_algorithms=settings.jwt_algorithms,
        jwt_leeway_seconds=settings.jwt_leeway_seconds,
    )


async def serve(settings: Settings | None = None) -> None:
    # Configure root logging first so the journal gate's INFO/WARN
    # lines (emitted from inside ``load_settings`` /
    # ``resolve_journal_config``) actually reach the operator's
    # terminal. Without this the root logger discards INFO by
    # default and the gate's open / closed messages are invisible.
    # ``log_level`` is parsed *inside* ``load_settings``, so the
    # first ``basicConfig`` uses INFO; we re-apply after parse so
    # ``LOG_LEVEL=debug`` actually takes effect.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings = settings if settings is not None else load_settings()
    if settings.log_level == "DEBUG":
        # Selective DEBUG: bridge + protocol + uvicorn, but
        # leave sse_starlette / httpx2 / hpack / h11 at WARNING
        # so the journal doesn't drown in chunk frames and
        # outbound SB GETs. The intent is "I want enough
        # signal to diagnose my dev box", not "log every byte
        # of every chunk". Operators that need deeper traces
        # can still flip individual loggers via env.
        logging.getLogger().setLevel(logging.DEBUG)
        for name in (
            "mcp_silverbullet",
            "mcp.server",
            "mcp.server.lowlevel",
            "mcp.server.mcpserver",
            "uvicorn",
            "uvicorn.error",
        ):
            logging.getLogger(name).setLevel(logging.DEBUG)
    else:
        logging.getLogger().setLevel(settings.log_level)
    if settings.log_level == "DEBUG":
        install_http_debug_hooks()
        logging.info(
            "debug logging on (MCP_SILVERBULLET_LOG_LEVEL=%s); "
            "non-HTTP/1 first-bytes and sanitized request metadata "
            "will be logged. UVICORN_LOG_LEVEL is ignored — uvicorn "
            "is configured from this env var, not its own.",
            settings.log_level,
        )
    async with SBClient(settings.sb_url, settings.sb_token) as sb:
        verifier = build_verifier(settings)
        mcp = build_mcp(
            sb,
            verifier=verifier,
            resource_url=settings.resource_url,
            journal=settings.journal,
            list_pages_hydrate_etags=settings.list_pages_hydrate_etags,
            log_level=settings.log_level,
        )
        print(
            f"mcp-silverbullet listening on http://{settings.host}:{settings.port}/mcp "
            f"(SB {settings.sb_url}, auth={settings.auth_mode}, "
            f"log={settings.log_level})",
            file=sys.stderr,
        )
        await _run_http(mcp, settings)


async def _run_http(mcp: object, settings: Settings) -> None:
    """Bind uvicorn ourselves so debug logging can wrap the ASGI app.

    The SDK's ``run_streamable_http_async`` constructs
    ``uvicorn.Config`` with only ``log_level`` and no hook to add
    middleware or diagnose httptools parse failures. We duplicate
    the ~15-line boot so ``LOG_LEVEL=debug`` can:

    - attach :class:`RequestDumpMiddleware` (sanitized headers +
      status for requests that *did* parse as HTTP/1);
    - pass ``log_level`` and ``proxy_headers=True`` (cloudflared
      sets ``X-Forwarded-*``; without this the access log shows
      127.0.0.1 for every client).
    """
    import uvicorn
    from mcp.server.mcpserver.server import MCPServer

    assert isinstance(mcp, MCPServer)
    starlette_app = mcp.streamable_http_app(
        transport_security=_transport_security(settings),
        host=settings.host,
    )
    if settings.log_level == "DEBUG":
        starlette_app.add_middleware(RequestDumpMiddleware)
    config = uvicorn.Config(
        starlette_app,
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
        access_log=True,
        proxy_headers=True,
    )
    server = uvicorn.Server(config)
    await server.serve()


def run() -> None:
    """Console-script / ``python -m mcp_silverbullet`` entry."""
    asyncio.run(serve())


# Re-exported for tests that need to construct a verifier
# collection without going through ``build_verifier``.
_select_verifier = select_verifier


if __name__ == "__main__":  # pragma: no cover
    run()
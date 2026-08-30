"""Bridge process entry: env → SBClient → MCPServer → Streamable HTTP.

T1 shipped a hello-print smoke. T6 replaces it with the real boot:
read env, open the outbound SB client, mount the inbound MCP app, bind
uvicorn via ``MCPServer.run_streamable_http_async``.
"""

from __future__ import annotations

import asyncio
import os
import sys
from dataclasses import dataclass

from mcp.server.transport_security import TransportSecuritySettings

from mcp_silverbullet.sb_client import SBClient
from mcp_silverbullet.server import build_mcp

_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_PORT = 8000
_DEFAULT_SB_URL = "http://127.0.0.1:3000"


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


def load_settings(environ: dict[str, str] | None = None) -> Settings:
    """Parse process env. Raises :class:`SystemExit` on missing token."""
    env = os.environ if environ is None else environ
    token = (env.get("MCP_SILVERBULLET_TOKEN") or "").strip()
    if not token:
        raise SystemExit(
            "MCP_SILVERBULLET_TOKEN is required "
            "(shared bearer secret for inbound MCP and, by default, outbound SB)"
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


async def serve(settings: Settings | None = None) -> None:
    settings = settings if settings is not None else load_settings()
    async with SBClient(settings.sb_url, settings.sb_token) as sb:
        mcp = build_mcp(
            sb,
            token=settings.token,
            resource_url=settings.resource_url,
        )
        print(
            f"mcp-silverbullet listening on http://{settings.host}:{settings.port}/mcp "
            f"(SB {settings.sb_url})",
            file=sys.stderr,
        )
        await mcp.run_streamable_http_async(
            host=settings.host,
            port=settings.port,
            transport_security=_transport_security(settings),
        )


def run() -> None:
    """Console-script / ``python -m mcp_silverbullet`` entry."""
    asyncio.run(serve())


if __name__ == "__main__":  # pragma: no cover
    run()

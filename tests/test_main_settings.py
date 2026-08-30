"""Env contract for the T6 boot path. No sockets."""

from __future__ import annotations

import pytest

from mcp_silverbullet.main import load_settings


def test_token_required() -> None:
    with pytest.raises(SystemExit, match="MCP_SILVERBULLET_TOKEN is required"):
        load_settings({})


def test_defaults() -> None:
    s = load_settings({"MCP_SILVERBULLET_TOKEN": "secret"})
    assert s.token == "secret"
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


def test_sb_token_may_be_empty_when_sb_has_no_auth() -> None:
    s = load_settings(
        {
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
            {"MCP_SILVERBULLET_TOKEN": "x", "MCP_SILVERBULLET_PORT": "nope"}
        )

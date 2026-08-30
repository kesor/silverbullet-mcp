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
    # T28: no ``MCP_SILVERBULLET_LIST_PAGES_HYDRATE_ETAGS`` env →
    # etag-hydration off (the v1.1 default); operators opt in
    # explicitly because hydration is an N+1 against SB.
    assert s.list_pages_hydrate_etags is False


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
            "MCP_SILVERBULLET_TOKEN": "x",
            "MCP_SILVERBULLET_LIST_PAGES_HYDRATE_ETAGS": raw,
        }
    )
    assert s.list_pages_hydrate_etags is True


@pytest.mark.parametrize("raw", ["", "0", "no", "false", "off", "anything-else"])
def test_list_pages_hydrate_etags_other_values_disable(raw: str) -> None:
    """Anything else (including empty string and unset) disables hydration.

    The default is off (the v1.1 wire shape, no per-page round
    trips); operators opt in explicitly. A typo in the env var
    name (``LIST_PAGES_HYDRATE_ETAG`` without the trailing ``S``)
    surfaces as ``False`` rather than crashing the bridge — the
    operator's existing ``list_pages`` calls keep working, the
    hydration just doesn't happen.
    """
    s = load_settings(
        {
            "MCP_SILVERBULLET_TOKEN": "x",
            "MCP_SILVERBULLET_LIST_PAGES_HYDRATE_ETAGS": raw,
        }
    )
    assert s.list_pages_hydrate_etags is False

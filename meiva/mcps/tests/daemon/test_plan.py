from __future__ import annotations

from typing import Any, Literal, cast

import pytest

from meiva.mcps._plan import (
    DirectConnect,
    HttpConnect,
    StdioConnect,
    plan_connect,
    unmet_requirement,
)


def plan(spec: dict[str, Any], *, url: str | None = None) -> object:
    return plan_connect("srv", spec, url=url)


def test_a_command_entry_is_a_stdio_child() -> None:
    assert plan({"command": "npx", "args": ["-y", "tool"], "env": {"K": "v"}}) == StdioConnect(
        command="npx", args=("-y", "tool"), env={"K": "v"}
    )


def test_a_command_entry_without_args_or_env_declares_none() -> None:
    assert plan({"command": "x"}) == StdioConnect(command="x", args=(), env=None)
    assert plan({"command": "x", "env": {}}) == StdioConnect(command="x", args=(), env=None)


# meiva:note
# Sharing the child among connections is the session cache's concern, not the transport's.
@pytest.mark.parametrize("shared", [True, False, None])
def test_shared_true_or_absent_is_still_a_stdio_child(shared: bool | None) -> None:
    assert isinstance(plan({"command": "x", "shared": shared}), StdioConnect)


def test_a_url_entry_is_an_http_endpoint_with_static_headers() -> None:
    assert plan(
        {"url": "https://mcp.example.com/mcp", "headers": {"Authorization": "Bearer k"}},
        url="https://mcp.example.com/mcp",
    ) == HttpConnect(
        server="srv",
        url="https://mcp.example.com/mcp",
        headers={"Authorization": "Bearer k"},
        oauth=False,
    )


def test_an_oauth_url_entry_asks_for_the_authorization_flow() -> None:
    result = plan(
        {"url": "https://mcp.example.com/mcp", "oauth": True}, url="https://mcp.example.com/mcp"
    )
    assert result == HttpConnect(
        server="srv", url="https://mcp.example.com/mcp", headers=None, oauth=True
    )


def test_a_url_wins_over_a_shared_declaration() -> None:
    result = plan({"shared": "browser"}, url="https://mcp.example.com/mcp")
    assert isinstance(result, HttpConnect)


# meiva:note
# `"shared": "browser"` / `"computer_use"` need no child: the daemon dials the service.
@pytest.mark.parametrize("service", ["browser", "computer_use"])
def test_a_shared_service_entry_is_dialed_directly(service: str) -> None:
    expected = DirectConnect(cast(Literal["browser", "computer_use"], service))
    assert plan({"command": ".venv/bin/python", "shared": service}) == expected


# meiva:note
# Silently falling back to a per-connection child would defeat the declared intent.
def test_an_unknown_shared_value_fails_fast() -> None:
    with pytest.raises(ValueError, match="unknown shared value 'mars'"):
        plan({"command": "x", "shared": "mars"})


def test_a_command_entry_must_name_its_command() -> None:
    with pytest.raises(KeyError):
        plan({"args": ["x"]})
    with pytest.raises(TypeError, match="command must be a string"):
        plan({"command": ["x"]})


@pytest.mark.parametrize(
    ("spec", "message"),
    [
        ({"command": "x", "args": "-y tool"}, "args must be a list of strings"),
        ({"command": "x", "args": ["a", 1]}, "args must be a list of strings"),
        ({"command": "x", "env": ["K=v"]}, "env must map strings to strings"),
        ({"command": "x", "env": {"K": 1}}, "env must map strings to strings"),
    ],
)
def test_malformed_args_or_env_never_reach_the_child(spec: dict[str, Any], message: str) -> None:
    with pytest.raises(TypeError, match=message):
        plan(spec)


def test_a_display_requirement_is_unmet_on_a_headless_host() -> None:
    reason = unmet_requirement({"display": True}, display_available=False)
    assert reason == "MCP server requires a display, but this host has none (headless server)"


@pytest.mark.parametrize(
    ("requires", "display_available"),
    [({"display": True}, True), ({"display": False}, False), ({}, False), ({}, True)],
)
def test_every_other_requirement_combination_is_met(
    requires: dict[str, bool], display_available: bool
) -> None:
    assert unmet_requirement(requires, display_available=display_available) is None

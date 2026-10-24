from __future__ import annotations

import pytest

from ops.inventory.toggles import (
    ToggleVerdicts,
    Verdict,
    mcp_summary,
    plan_toggles,
    require_agent_runner,
    servers_to_probe,
)

_OK = Verdict(ok=True, reason=None)


# meiva:note
# The inventory ops run on an agent-runner, alone or beside the gateway role, and nowhere else.
def test_only_an_agent_runner_may_run_the_inventory_ops() -> None:
    require_agent_runner(frozenset({"agent-runner"}))
    require_agent_runner(frozenset({"gateway", "agent-runner"}))

    with pytest.raises(RuntimeError, match=r"only on an agent-runner, not \['gateway'\]"):
        require_agent_runner(frozenset({"gateway"}))


# meiva:note
# An MCP entry reads as its url, or as its command followed by its arguments; blanks drop out.
@pytest.mark.parametrize(
    ("spec", "summary"),
    [
        ({"url": "https://mcp.example/sse"}, "https://mcp.example/sse"),
        ({"command": "run-fs", "args": ["--root", "/"]}, "run-fs --root /"),
        ({"command": "run-fs"}, "run-fs"),
        ({"args": ["--only-args"]}, "--only-args"),
        ({"command": "x", "args": "not-a-list"}, "x"),
        ({"url": "https://u", "command": "ignored"}, "https://u"),
    ],
)
def test_an_mcp_entry_summarises_to_one_line(spec: object, summary: str) -> None:
    assert mcp_summary(spec) == summary


# meiva:note
# A value that is not a mapping is reported as malformed instead of failing the listing.
@pytest.mark.parametrize("spec", [None, "run-fs", ["run-fs"], 7])
def test_a_malformed_mcp_entry_is_labelled(spec: object) -> None:
    assert mcp_summary(spec) == "(malformed entry)"


# meiva:note
# Only a known server that is being enabled needs the host capability verdict.
def test_servers_to_probe_are_the_known_servers_being_enabled() -> None:
    requested = {"enable-known": True, "disable-known": False, "enable-ghost": True}

    assert servers_to_probe(requested, frozenset({"enable-known", "disable-known"})) == (
        "enable-known",
    )


# meiva:note
# Plugins must be installed; enabling and disabling an installed one are both fine.
def test_plugins_pass_only_when_installed() -> None:
    verdicts = plan_toggles(
        {"meiva_code": False, "ghost": True},
        {},
        installed_plugins=frozenset({"meiva_code"}),
        known_servers=frozenset(),
        capabilities={},
    )

    assert verdicts == ToggleVerdicts(
        plugins=(
            ("meiva_code", _OK),
            ("ghost", Verdict(ok=False, reason="not installed on this host")),
        ),
        mcp_servers=(),
        applied=False,
    )


# meiva:note
# An unknown server is rejected, a disable is always ok (even for a server whose requirements
# would block it), and an enable takes the capability verdict the shell probed.
def test_mcp_servers_follow_their_capability_only_when_enabled() -> None:
    blocked = Verdict(ok=False, reason="requires a display, but this host has none")

    verdicts = plan_toggles(
        {},
        {"ghost": True, "headed": True, "headless-off": False, "plain": True},
        installed_plugins=frozenset(),
        known_servers=frozenset({"headed", "headless-off", "plain"}),
        capabilities={"headed": blocked, "plain": _OK},
    )

    assert verdicts.mcp_servers == (
        ("ghost", Verdict(ok=False, reason="not a known MCP server on this host")),
        ("headed", blocked),
        ("headless-off", _OK),
        ("plain", _OK),
    )
    assert verdicts.applied is False


# meiva:note
# One refused toggle refuses the whole request, and an empty request is trivially applied.
def test_the_request_is_all_or_nothing() -> None:
    passing = plan_toggles(
        {"meiva_code": True},
        {"plain": False},
        installed_plugins=frozenset({"meiva_code"}),
        known_servers=frozenset({"plain"}),
        capabilities={},
    )
    empty = plan_toggles(
        {}, {}, installed_plugins=frozenset(), known_servers=frozenset(), capabilities={}
    )

    assert passing.applied is True
    assert empty == ToggleVerdicts(plugins=(), mcp_servers=(), applied=True)


# meiva:note
# An enable with no probed verdict is a caller bug and raises instead of defaulting to ok.
def test_an_unprobed_enable_is_refused() -> None:
    with pytest.raises(ValueError, match="no capability verdict for MCP server 'plain'"):
        plan_toggles(
            {},
            {"plain": True},
            installed_plugins=frozenset(),
            known_servers=frozenset({"plain"}),
            capabilities={},
        )

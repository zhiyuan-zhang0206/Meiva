from __future__ import annotations

from pathlib import Path

from base.lm.harness.claude.auth import AuthConfig, AuthMode
from base.lm.harness.claude.config import BridgeConfig
from base.lm.harness.claude.options import option_values

ENVIRON = {"OPENAI_API_KEY": "must-not-ride", "ANTHROPIC_API_KEY": "must-not-ride-either"}


def test_the_values_are_the_boundary_the_cli_runs_under(tmp_path: Path) -> None:
    config = BridgeConfig(
        cwd=tmp_path, model="model-x", tools=("Read", "Bash"), permission_mode="plan"
    )

    values = option_values(
        config,
        environ=ENVIRON,
        cli_path="/opt/claude/bin/claude",
        option_fields=("thinking", "model"),
        resume="sess-9",
    )

    assert values["cli_path"] == "/opt/claude/bin/claude"
    assert values["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert values["model"] == "model-x"
    assert values["cwd"] == str(tmp_path)
    assert values["tools"] == ["Read", "Bash"]
    assert values["permission_mode"] == "plan"
    assert values["max_turns"] == 24
    assert values["setting_sources"] == []
    assert values["strict_mcp_config"] is True
    assert values["include_partial_messages"] is True
    assert values["resume"] == "sess-9"


def test_the_child_environment_is_the_allowlist_built_from_what_the_caller_hands_in(
    tmp_path: Path,
) -> None:
    values = option_values(
        BridgeConfig(cwd=tmp_path),
        environ=ENVIRON,
        cli_path=None,
        option_fields=(),
        resume=None,
    )

    env = values["env"]
    assert isinstance(env, dict)
    assert env["OPENAI_API_KEY"] == ""
    assert env["ANTHROPIC_API_KEY"] == ""


def test_the_two_conditional_options_ride_only_when_they_are_supported(tmp_path: Path) -> None:
    without = option_values(
        BridgeConfig(cwd=tmp_path),
        environ={},
        cli_path=None,
        option_fields=("model",),
        resume=None,
    )
    with_field = option_values(
        BridgeConfig(cwd=tmp_path),
        environ={},
        cli_path=None,
        option_fields=("thinking",),
        resume=None,
    )

    assert "cli_path" not in without
    assert "thinking" not in without
    assert with_field["thinking"] == {"type": "adaptive", "display": "summarized"}


def test_an_unset_tool_list_stays_unset_and_a_set_one_is_a_list(tmp_path: Path) -> None:
    unset = option_values(
        BridgeConfig(cwd=tmp_path), environ={}, cli_path=None, option_fields=(), resume=None
    )
    listed = option_values(
        BridgeConfig(cwd=tmp_path, tools=("Read",)),
        environ={},
        cli_path=None,
        option_fields=(),
        resume=None,
    )

    assert unset["tools"] is None
    assert listed["tools"] == ["Read"]


def test_the_api_key_mode_restores_exactly_the_variable_the_caller_named(
    tmp_path: Path,
) -> None:
    values = option_values(
        BridgeConfig(cwd=tmp_path, auth=AuthConfig(mode=AuthMode.API_KEY)),
        environ={"ANTHROPIC_API_KEY": "sk-live"},
        cli_path=None,
        option_fields=(),
        resume=None,
    )

    env = values["env"]
    assert isinstance(env, dict)
    assert env["ANTHROPIC_API_KEY"] == "sk-live"

from __future__ import annotations

from pathlib import Path

import pytest

from base.lm.harness.claude.auth import AuthConfig, AuthMode
from base.lm.harness.claude.config import BridgeConfig
from base.lm.harness.claude.options import option_values

ENVIRON = {"OPENAI_API_KEY": "must-not-ride", "ANTHROPIC_API_KEY": "must-not-ride-either"}


def test_selected_effort_reaches_the_claude_sdk(tmp_path: Path) -> None:
    config = BridgeConfig(cwd=tmp_path, model="claude-opus-5-5", reasoning_effort="low")
    values = option_values(
        config,
        environ={},
        cli_path=None,
        option_fields=("extra_args", "thinking", "effort", "system_prompt"),
        resume="previous",
    )
    assert values["model"] == "claude-opus-5-5"
    assert values["effort"] == "low"
    assert values["resume"] == "previous"
    with pytest.raises(ValueError, match="does not support the selected effort"):
        option_values(
            config,
            environ={},
            cli_path=None,
            option_fields=(
                "extra_args",
                "system_prompt",
            ),
            resume=None,
        )


def test_the_values_are_the_boundary_the_cli_runs_under(tmp_path: Path) -> None:
    config = BridgeConfig(
        cwd=tmp_path, model="model-x", tools=("Read", "Bash"), permission_mode="plan"
    )

    values = option_values(
        config,
        environ=ENVIRON,
        cli_path="/opt/claude/bin/claude",
        option_fields=("extra_args", "thinking", "model", "system_prompt"),
        resume="sess-9",
    )

    assert values["cli_path"] == "/opt/claude/bin/claude"
    assert values["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert values["model"] == "model-x"
    assert values["cwd"] == str(tmp_path)
    assert values["tools"] == ["Read", "Bash"]
    assert values["permission_mode"] == "plan"
    assert "max_turns" not in values
    assert "setting_sources" not in values
    assert "strict_mcp_config" not in values
    assert values["include_partial_messages"] is True
    assert values["resume"] == "sess-9"
    assert values["system_prompt"] == {"type": "preset", "preset": "claude_code"}
    assert "forward_subagent_text" not in values


def test_the_child_environment_is_the_allowlist_built_from_what_the_caller_hands_in(
    tmp_path: Path,
) -> None:
    values = option_values(
        BridgeConfig(cwd=tmp_path),
        environ=ENVIRON,
        cli_path=None,
        option_fields=(
            "extra_args",
            "system_prompt",
        ),
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
        option_fields=("extra_args", "model", "system_prompt"),
        resume=None,
    )
    with_field = option_values(
        BridgeConfig(cwd=tmp_path),
        environ={},
        cli_path=None,
        option_fields=("extra_args", "thinking", "system_prompt"),
        resume=None,
    )

    assert "cli_path" not in without
    assert "thinking" not in without
    assert with_field["thinking"] == {"type": "adaptive", "display": "summarized"}


def test_an_unset_tool_list_stays_unset_and_a_set_one_is_a_list(tmp_path: Path) -> None:
    unset = option_values(
        BridgeConfig(cwd=tmp_path),
        environ={},
        cli_path=None,
        option_fields=(
            "extra_args",
            "system_prompt",
        ),
        resume=None,
    )
    listed = option_values(
        BridgeConfig(cwd=tmp_path, tools=("Read",)),
        environ={},
        cli_path=None,
        option_fields=(
            "extra_args",
            "system_prompt",
        ),
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
        option_fields=(
            "extra_args",
            "system_prompt",
        ),
        resume=None,
    )

    env = values["env"]
    assert isinstance(env, dict)
    assert env["ANTHROPIC_API_KEY"] == "sk-live"


def test_an_sdk_without_the_system_prompt_option_fails_instead_of_running_on_the_stub(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="does not support the system prompt preset"):
        option_values(
            BridgeConfig(cwd=tmp_path),
            environ={},
            cli_path=None,
            option_fields=("extra_args", "thinking", "model"),
            resume=None,
        )


def test_an_sdk_that_cannot_pass_replay_user_messages_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="replay-user-messages"):
        _ = option_values(
            BridgeConfig(cwd=tmp_path),
            environ={},
            cli_path=None,
            option_fields=("system_prompt",),
            resume=None,
        )


def test_every_session_asks_the_cli_to_echo_user_messages(tmp_path: Path) -> None:
    values = option_values(
        BridgeConfig(cwd=tmp_path),
        environ={},
        cli_path=None,
        option_fields=("extra_args", "system_prompt"),
        resume=None,
    )
    assert values["extra_args"] == {"replay-user-messages": None}

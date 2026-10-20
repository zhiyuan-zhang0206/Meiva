from __future__ import annotations

from pathlib import Path

import pytest

from base.lm.harness.claude.auth import (
    AuthConfig,
    AuthMode,
    MissingApiKeyError,
    build_env,
    describe_auth,
)

SAMPLE_KEY = "sk-ant-sample-value"


def test_oauth_blanks_both_credential_variables() -> None:
    env = build_env(
        {"ANTHROPIC_API_KEY": SAMPLE_KEY, "CLAUDE_CODE_OAUTH_TOKEN": "tok", "PATH": "/bin"},
        AuthConfig(),
    )
    assert env["ANTHROPIC_API_KEY"] == ""
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == ""
    assert env["PATH"] == "/bin", "the caller's environment view still rides along"


# meiva:note
# The bridge hands each session a registration handle; a tool the CLI spawns needs it
# to attach as the agent it belongs to. A live turn found it dropped here, so its own
# Bash could not say which agent it was.
# meiva:link base/lm/harness/claude/docs/claude.meiva.okf.md
def test_the_registration_handle_reaches_the_cli() -> None:
    env = build_env({"MEIVA_HARNESS_HANDLE": "handle-1", "MEIVA_HOME": "/home"}, AuthConfig())
    assert env["MEIVA_HARNESS_HANDLE"] == "handle-1"
    assert env["MEIVA_HOME"] == "/home"


def test_api_key_mode_passes_exactly_one_secret() -> None:
    env = build_env(
        {"ANTHROPIC_API_KEY": SAMPLE_KEY, "CLAUDE_CODE_OAUTH_TOKEN": "tok"},
        AuthConfig(mode=AuthMode.API_KEY),
    )
    assert env["ANTHROPIC_API_KEY"] == SAMPLE_KEY
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == ""


def test_api_key_mode_without_a_key_fails_fast() -> None:
    with pytest.raises(MissingApiKeyError):
        build_env({}, AuthConfig(mode=AuthMode.API_KEY))


def test_config_dir_is_forwarded_only_when_asked() -> None:
    assert "CLAUDE_CONFIG_DIR" not in build_env({}, AuthConfig())
    env = build_env({}, AuthConfig(config_dir=Path("/workspace/claude-config")))
    assert env["CLAUDE_CONFIG_DIR"] == "/workspace/claude-config"


def test_describe_never_contains_the_secret() -> None:
    described = describe_auth(AuthConfig(), {"ANTHROPIC_API_KEY": SAMPLE_KEY})
    assert described == {"mode": "oauth", "config_dir": "default", "api_key_present": True}
    assert SAMPLE_KEY not in repr(described)

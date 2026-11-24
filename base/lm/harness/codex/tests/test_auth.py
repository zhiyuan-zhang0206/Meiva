from __future__ import annotations

from base.lm.harness.codex.auth import SECRET_ENV_NAMES, build_env

SAMPLE_KEY = "sk-proj-sample-value"


def test_the_key_never_reaches_the_child_by_default() -> None:
    env = build_env({"OPENAI_API_KEY": SAMPLE_KEY, "PATH": "/bin", "CODEX_HOME": "/h"})
    assert "OPENAI_API_KEY" not in env
    assert env["PATH"] == "/bin", "the rest of the caller's environment view rides along"
    assert env["CODEX_HOME"] == "/h"


def test_every_credential_variable_the_cli_reads_is_covered() -> None:
    env = build_env(dict.fromkeys(SECRET_ENV_NAMES, SAMPLE_KEY))
    assert env == {}


def test_api_key_mode_passes_the_key_through() -> None:
    env = build_env({"OPENAI_API_KEY": SAMPLE_KEY}, api_key_mode=True)
    assert env["OPENAI_API_KEY"] == SAMPLE_KEY


def test_the_zero_trace_git_remotes_setting_and_the_git_shim_path_reach_the_cli() -> None:
    env = build_env({"MEIVA_ZERO_TRACE_GIT_REMOTES": "example-corp/", "PATH": "/shim:/bin"})
    assert env["MEIVA_ZERO_TRACE_GIT_REMOTES"] == "example-corp/"
    assert env["PATH"] == "/shim:/bin"

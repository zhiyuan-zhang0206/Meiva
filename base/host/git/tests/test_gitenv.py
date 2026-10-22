from __future__ import annotations

from base.host.git.gitenv import git_env


def test_prompts_are_off_and_ssh_is_bounded() -> None:
    env = git_env({})
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GIT_SSH_COMMAND"] == "ssh -o BatchMode=yes -o ConnectTimeout=10"


def test_overrides_an_inherited_ssh_command() -> None:
    env = git_env(
        {
            "GIT_SSH_COMMAND": "ssh -i /tmp/inherited-key",
            "GIT_TERMINAL_PROMPT": "1",
        }
    )
    assert env["GIT_SSH_COMMAND"].startswith("ssh -o BatchMode=yes")
    assert env["GIT_TERMINAL_PROMPT"] == "0"


def test_carries_the_rest_of_the_environment() -> None:
    assert git_env({"MEIVA_GITENV_CANARY": "kept"})["MEIVA_GITENV_CANARY"] == "kept"

from pathlib import Path

import pytest

from base.host.env.agent_env import AGENT_BIRTH_CONFIG_ENV, AGENT_CONFIG_OVERLAY_ENV
from base.sessions.exec_host.child_environment import ChildExecution, build_child_environment


@pytest.mark.parametrize(
    ("cwd", "keeps_virtual_env"),
    [
        ("source/agent", True),
        ("source", True),
        ("outside", False),
        ("source/.worktrees/feature", False),
        ("source/.claude/worktrees/feature", False),
    ],
)
def test_a_child_keeps_the_virtual_env_only_inside_its_own_checkout(
    tmp_path: Path, cwd: str, keeps_virtual_env: bool
) -> None:
    environ = {"VIRTUAL_ENV": "/source/.venv", "PATH": "/bin"}

    env = build_child_environment(
        environ,
        cwd=tmp_path / cwd,
        source_root=tmp_path / "source",
        execution=ChildExecution(None, tmp_path / "request", tmp_path / "result"),
        config_overlay_json=None,
        birth_config_json=None,
    )

    assert ("VIRTUAL_ENV" in env) is keeps_virtual_env
    assert environ == {"VIRTUAL_ENV": "/source/.venv", "PATH": "/bin"}


def test_an_interpreter_outside_a_checkout_keeps_the_virtual_env_anywhere(
    tmp_path: Path,
) -> None:
    env = build_child_environment(
        {"VIRTUAL_ENV": "/source/.venv"},
        cwd=tmp_path / "anywhere",
        source_root=None,
        execution=ChildExecution(None, tmp_path / "request", tmp_path / "result"),
        config_overlay_json=None,
        birth_config_json=None,
    )

    assert env["VIRTUAL_ENV"] == "/source/.venv"


@pytest.mark.parametrize(
    ("config_overlay_json", "birth_config_json"),
    [(None, None), ('{"model": "pinned"}', '{"seed": 1}')],
)
def test_only_explicit_config_carriers_bind_the_child(
    tmp_path: Path, config_overlay_json: str | None, birth_config_json: str | None
) -> None:
    env = build_child_environment(
        {AGENT_CONFIG_OVERLAY_ENV: "ambient-overlay", AGENT_BIRTH_CONFIG_ENV: "ambient-birth"},
        cwd=tmp_path,
        source_root=None,
        execution=ChildExecution(42, tmp_path / "request", tmp_path / "result"),
        config_overlay_json=config_overlay_json,
        birth_config_json=birth_config_json,
    )

    assert env.get(AGENT_CONFIG_OVERLAY_ENV) == config_overlay_json
    assert env.get(AGENT_BIRTH_CONFIG_ENV) == birth_config_json
    assert env["MEIVA_AGENT_ID"] == "42"
    assert env["MEIVA_PROCESS_PROFILE"] == "agent"
    assert env["MEIVA_EXEC_REQUEST_FILE"] == str(tmp_path / "request")
    assert env["MEIVA_EXEC_RESULT_FILE"] == str(tmp_path / "result")

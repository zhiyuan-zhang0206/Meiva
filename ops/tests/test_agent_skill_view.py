from __future__ import annotations

import contextlib
import json
import subprocess
from collections.abc import Generator
from pathlib import Path
from typing import Any, cast

import psycopg
import pytest
from langgraph.checkpoint.base import empty_checkpoint
from langgraph.checkpoint.postgres import PostgresSaver
from psycopg.types.json import Jsonb

from base.db import create_agent
from base.packages.extensions.install_registry import InstalledPackage, register
from base.packages.plugins.mcp_enabled import set_mcp_enabled
from base.paths import meiva_home, skills_dir
from ops.cluster import operations as cluster


def _write_skill(root: Path, name: str, description: str = "project skill") -> None:
    skill = root / name
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n# {name}\n",
        encoding="utf-8",
    )


@pytest.fixture
def load_dir(unit_home: Path) -> Path:
    root = skills_dir(meiva_home())
    root.mkdir()
    _write_skill(root, "load-skill", "converged skill")
    _write_skill(root, "unregistered-skill", "must not appear")
    register(InstalledPackage(name="load-skill", type="skill"))
    set_mcp_enabled("chrome", enabled=False)
    set_mcp_enabled("computer_use", enabled=False)
    return root


def _project_with_skill(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "project"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    project_skills = repo / ".agents" / "skills"
    _write_skill(project_skills, "project-skill")
    return repo, project_skills


def _checkpoint_cwd(conn: psycopg.Connection, agent_id: int, cwd: Path) -> None:
    checkpoint = empty_checkpoint()
    checkpoint["channel_values"] = {"meiva_code__cwd": str(cwd)}
    checkpoint["channel_versions"] = {"meiva_code__cwd": "1", "__start__": "1"}
    saver = PostgresSaver(conn=cast(Any, conn))
    saver.put(
        config={"configurable": {"thread_id": str(agent_id), "checkpoint_ns": ""}},
        checkpoint=checkpoint,
        metadata={"source": "input", "step": 1, "parents": {}},
        new_versions={"meiva_code__cwd": "1"},
    )


def _agent(conn: psycopg.Connection, cwd: Path | None, wanted: list[str]) -> int:
    agent_id = create_agent(conn)
    conn.execute(
        "INSERT INTO agents_meta (id, status, config_overlay, birth_config) VALUES (%s, 'running', %s, %s)",
        (
            agent_id,
            Jsonb({"skills_to_inject_into_system_prompt": wanted}),
            Jsonb({"skills_to_inject_into_system_prompt": ["birth-skill"]}),
        ),
    )
    if cwd is not None:
        _checkpoint_cwd(conn, agent_id, cwd)
    return agent_id


class _OneConnectionPool:
    def __init__(self, conn: psycopg.Connection) -> None:
        self._conn = conn

    @contextlib.contextmanager
    def connection(self) -> Generator[psycopg.Connection, None, None]:
        yield self._conn


def test_agent_skill_view_inputs_read_checkpointed_cwd_and_pinned_narrowing(
    db_conn: psycopg.Connection, tmp_path: Path
) -> None:
    agent_id = _agent(db_conn, tmp_path, ["overlay-skill"])

    cwd, wanted = cluster.agent_skill_view_inputs(_OneConnectionPool(db_conn), agent_id)

    assert cwd == tmp_path
    assert wanted == ["overlay-skill"]


def test_agent_skill_view_missing_row_is_load_dir_only(
    db_conn: psycopg.Connection, load_dir: Path
) -> None:
    view = cluster.agent_skill_view_op(999_999_999, _OneConnectionPool(db_conn))

    assert [command.name for command in view.commands] == ["compact", "load-skill", "recap"]


def test_agent_skill_view_includes_project_skills_for_persisted_cwd(
    db_conn: psycopg.Connection, tmp_path: Path, load_dir: Path
) -> None:
    repo, _ = _project_with_skill(tmp_path)
    agent_id = _agent(db_conn, repo, ["*"])

    view = cluster.agent_skill_view_op(agent_id, _OneConnectionPool(db_conn))

    assert [command.name for command in view.commands] == [
        "compact",
        "load-skill",
        "project-skill",
        "recap",
    ]


def test_agent_skill_view_without_cwd_or_repo_is_load_dir_only(
    db_conn: psycopg.Connection, tmp_path: Path, load_dir: Path
) -> None:
    no_cwd_id = _agent(db_conn, None, ["*"])
    non_repo_id = _agent(db_conn, tmp_path / "not-a-repo", ["*"])
    pool = _OneConnectionPool(db_conn)

    missing_cwd_view = cluster.agent_skill_view_op(no_cwd_id, pool)
    non_repo_view = cluster.agent_skill_view_op(non_repo_id, pool)

    assert [command.name for command in missing_cwd_view.commands] == [
        "compact",
        "load-skill",
        "recap",
    ]
    assert [command.name for command in non_repo_view.commands] == [
        "compact",
        "load-skill",
        "recap",
    ]


def test_agent_skill_view_clears_project_provider_after_each_op(
    db_conn: psycopg.Connection, tmp_path: Path, load_dir: Path
) -> None:
    repo, _ = _project_with_skill(tmp_path)
    project_agent = _agent(db_conn, repo, ["*"])
    local_agent = _agent(db_conn, None, ["*"])
    pool = _OneConnectionPool(db_conn)

    assert [
        command.name for command in cluster.agent_skill_view_op(project_agent, pool).commands
    ] == ["compact", "load-skill", "project-skill", "recap"]
    assert [
        command.name for command in cluster.agent_skill_view_op(local_agent, pool).commands
    ] == ["compact", "load-skill", "recap"]


def test_agent_skill_view_honors_per_agent_skill_narrowing(
    db_conn: psycopg.Connection, load_dir: Path
) -> None:
    _write_skill(load_dir, "other-skill", "other converged skill")
    register(InstalledPackage(name="other-skill", type="skill"))
    agent_id = _agent(db_conn, None, ["load_skill"])

    view = cluster.agent_skill_view_op(agent_id, _OneConnectionPool(db_conn))

    assert [command.name for command in view.commands] == ["compact", "load-skill", "recap"]


def test_agent_skill_view_includes_sorted_enabled_mcp_names(
    db_conn: psycopg.Connection, load_dir: Path
) -> None:
    (load_dir.parent / "mcp.json").write_text(
        json.dumps({"mcpServers": {"zeta": {"command": "z"}, "alpha": {"command": "a"}}}),
        encoding="utf-8",
    )
    set_mcp_enabled("zeta", enabled=True)
    agent_id = _agent(db_conn, None, ["*"])

    view = cluster.agent_skill_view_op(agent_id, _OneConnectionPool(db_conn))

    assert view.mcp_names == ["alpha", "zeta"]


def test_agent_skill_view_excludes_disabled_mcp_names(
    db_conn: psycopg.Connection, load_dir: Path
) -> None:
    (load_dir.parent / "mcp.json").write_text(
        json.dumps({"mcpServers": {"disabled": {"command": "off"}, "enabled": {"command": "on"}}}),
        encoding="utf-8",
    )
    set_mcp_enabled("disabled", enabled=False)
    agent_id = _agent(db_conn, None, ["*"])

    view = cluster.agent_skill_view_op(agent_id, _OneConnectionPool(db_conn))

    assert view.mcp_names == ["enabled"]


def test_agent_skill_view_without_machine_mcp_config_returns_empty_names(
    db_conn: psycopg.Connection, load_dir: Path
) -> None:
    agent_id = _agent(db_conn, None, ["*"])

    view = cluster.agent_skill_view_op(agent_id, _OneConnectionPool(db_conn))

    assert view.mcp_names == []

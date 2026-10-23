from __future__ import annotations

import contextlib
import subprocess
from collections.abc import Callable, Generator, Sequence
from pathlib import Path
from typing import Any, cast

import psycopg
import pytest
from langgraph.checkpoint.base import empty_checkpoint
from langgraph.checkpoint.postgres import PostgresSaver
from psycopg.types.json import Jsonb

from base.db import create_agent
from base.packages import composer_commands
from base.packages.skills import scan_tree
from base.packages.skills.tree import Skill, flatten
from base.paths import meiva_home, skills_dir
from ops.cluster import operations as cluster
from ops.rpc_schemas import AgentSkillViewResult


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
    return root


def _project_with_skill(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "project"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    project_skills = repo / ".agents" / "skills"
    _write_skill(project_skills, "project-skill")
    return repo, project_skills


def _view_inputs(
    cwd: Path | None, wanted: list[str]
) -> Callable[[Any, int], tuple[Path | None, list[str] | None]]:
    def _inputs(_pool: Any, _agent_id: int) -> tuple[Path | None, list[str] | None]:
        return cwd, wanted

    return _inputs


def _mcp_source(
    servers: dict[str, dict[str, Any]],
) -> Callable[..., dict[str, dict[str, Any]]]:
    def _load(*, include_disabled: bool = False) -> dict[str, dict[str, Any]]:
        assert include_disabled is True
        return servers

    return _load


def _skill_loader(loadable: set[str]) -> Callable[[Sequence[Path]], list[Skill]]:
    def _load(provider_roots: Sequence[Path] = ()) -> list[Skill]:
        return flatten(scan_tree(skills_dir(meiva_home()), loadable, provider_roots))

    return _load


def _discover_skill_commands(skills_fn: Callable[[], Sequence[Any]]) -> list[Any]:
    out = composer_commands.skill_commands(skills_fn())
    return [out[name] for name in sorted(out)]


def _view_op(
    agent_id: int,
    pool: Any,
    *,
    inputs_fn: Callable[[Any, int], tuple[Path | None, list[str] | None]] | None = None,
    loadable: set[str] | None = None,
    mcp_servers: dict[str, dict[str, Any]] | None = None,
    mcp_enabled: dict[str, bool] | None = None,
) -> AgentSkillViewResult:
    return cluster.agent_skill_view_op(
        agent_id,
        pool,
        inputs_fn=inputs_fn,
        skill_loader=_skill_loader(loadable if loadable is not None else {"load-skill"}),
        command_discoverer=_discover_skill_commands,
        mcp_loader=_mcp_source(mcp_servers if mcp_servers is not None else {}),
        mcp_enabled_reader=lambda: mcp_enabled if mcp_enabled is not None else {},
    )


class _OneConnectionPool:
    def __init__(self, conn: psycopg.Connection) -> None:
        self._conn = conn

    @contextlib.contextmanager
    def connection(self) -> Generator[psycopg.Connection, None, None]:
        yield self._conn


def test_agent_skill_view_inputs_read_checkpointed_cwd_and_pinned_narrowing(
    db_conn: psycopg.Connection, tmp_path: Path
) -> None:
    agent_id = create_agent(db_conn)
    db_conn.execute(
        "INSERT INTO agents_meta (id, status, config_overlay, birth_config) VALUES (%s, 'running', %s, %s)",
        (
            agent_id,
            Jsonb({"skills_to_inject_into_system_prompt": ["overlay-skill"]}),
            Jsonb({"skills_to_inject_into_system_prompt": ["birth-skill"]}),
        ),
    )
    checkpoint = empty_checkpoint()
    checkpoint["channel_values"] = {"meiva_code__cwd": str(tmp_path)}
    checkpoint["channel_versions"] = {"meiva_code__cwd": "1", "__start__": "1"}
    saver = PostgresSaver(conn=cast(Any, db_conn))
    saver.put(
        config={"configurable": {"thread_id": str(agent_id), "checkpoint_ns": ""}},
        checkpoint=checkpoint,
        metadata={"source": "input", "step": 1, "parents": {}},
        new_versions={"meiva_code__cwd": "1"},
    )

    cwd, wanted = cluster.agent_skill_view_inputs(_OneConnectionPool(db_conn), agent_id)

    assert cwd == tmp_path
    assert wanted == ["overlay-skill"]


def test_agent_skill_view_missing_row_is_load_dir_only(
    db_conn: psycopg.Connection, load_dir: Path
) -> None:
    view = _view_op(999_999_999, _OneConnectionPool(db_conn))

    assert [command.name for command in view.commands] == ["load-skill"]


def test_agent_skill_view_includes_project_skills_for_persisted_cwd(
    tmp_path: Path, load_dir: Path
) -> None:
    repo, _ = _project_with_skill(tmp_path)

    view = _view_op(42, object(), inputs_fn=_view_inputs(repo, ["*"]))

    assert {command.name for command in view.commands} >= {"load-skill", "project-skill"}


def test_agent_skill_view_without_cwd_or_repo_is_load_dir_only(
    tmp_path: Path, load_dir: Path
) -> None:
    missing_cwd_view = _view_op(42, object(), inputs_fn=_view_inputs(None, ["*"]))
    non_repo_view = _view_op(42, object(), inputs_fn=_view_inputs(tmp_path / "not-a-repo", ["*"]))

    assert {command.name for command in missing_cwd_view.commands} == {"load-skill"}
    assert {command.name for command in non_repo_view.commands} == {"load-skill"}


def test_agent_skill_view_clears_project_provider_after_each_op(
    tmp_path: Path, load_dir: Path
) -> None:
    repo, _ = _project_with_skill(tmp_path)
    assert "project-skill" in {
        command.name
        for command in _view_op(42, object(), inputs_fn=_view_inputs(repo, ["*"])).commands
    }

    assert "project-skill" not in {
        command.name
        for command in _view_op(43, object(), inputs_fn=_view_inputs(None, ["*"])).commands
    }


def test_agent_skill_view_honors_per_agent_skill_narrowing(tmp_path: Path, load_dir: Path) -> None:
    _write_skill(load_dir, "other-skill", "other converged skill")

    view = _view_op(
        42,
        object(),
        inputs_fn=_view_inputs(None, ["load_skill"]),
        loadable={"load-skill", "other-skill"},
    )

    assert [command.name for command in view.commands] == ["load-skill"]


def test_agent_skill_view_includes_sorted_enabled_mcp_names(load_dir: Path) -> None:
    view = _view_op(
        42,
        object(),
        inputs_fn=_view_inputs(None, ["*"]),
        mcp_servers={"zeta": {"command": "z"}, "alpha": {"command": "a"}},
        mcp_enabled={"zeta": True},
    )

    assert view.mcp_names == ["alpha", "zeta"]


def test_agent_skill_view_excludes_disabled_mcp_names(load_dir: Path) -> None:
    view = _view_op(
        42,
        object(),
        inputs_fn=_view_inputs(None, ["*"]),
        mcp_servers={"disabled": {"command": "off"}, "enabled": {"command": "on"}},
        mcp_enabled={"disabled": False},
    )

    assert view.mcp_names == ["enabled"]


def test_agent_skill_view_without_mcp_config_returns_empty_names(load_dir: Path) -> None:
    view = _view_op(42, object(), inputs_fn=_view_inputs(None, ["*"]))

    assert view.mcp_names == []

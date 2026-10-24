from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from base.telemetry import EventPipeline
from cli.commands.converge.spec import ALL_ROLES, ConvergeCtx
from cli.commands.extensions import external_skills
from cli.commands.lifecycle import converge_host
from cli.database.runtime import CommandRuntime, UserSession
from cli.database.tests.cli_tests import operator_database as operator_database
from cli.database.tests.cli_tests import operator_pipeline as operator_pipeline
from cli.database.tests.command_inputs import command_runtime as command_runtime

SKILL_NAME = "meiva-guide"
PUBLISHED = ("meiva-guide", "impersonator-guide")


@pytest.fixture
def home(tmp_path: Path) -> Path:
    host_home = tmp_path / "home"
    host_home.mkdir()
    return host_home


def _write_source(repo: Path) -> None:
    for name in PUBLISHED:
        source = repo / "meiva_builtins" / "skills" / "platform" / name
        source.mkdir(parents=True)
        (source / "SKILL.md").write_text("operator guidance\n")


def _target(client_home: Path) -> Path:
    return client_home / "skills" / SKILL_NAME


# meiva:note
# The converge context `converge_host` builds for `repo` from one command invocation's owners.
def _converge_ctx(
    repo: Path,
    home: Path,
    *,
    operator_database: Callable[[], Any],
    producer: Callable[[], EventPipeline],
    command_runtime: CommandRuntime,
) -> ConvergeCtx:
    return ConvergeCtx(
        repo=repo,
        meiva_home=home / ".meiva",
        roles=None,
        config=command_runtime.config,
        machine=command_runtime.machine,
        database_factory=operator_database,
        producer=producer,
        helper=command_runtime.helper,
        host=command_runtime.host,
        session=UserSession(user_home=home, login_shell="/bin/zsh", interactive=False),
        timers=command_runtime.timers,
        services=frozenset(),
    )


# meiva:note
# Whether the unit is the host's prod install is decided from the home and the checkout by
# `is_prod_install` (tested beside it); this test hands `run_converge` each verdict.
def test_bridge_step_is_prod_host_global_and_skipped_for_dev_worktrees(
    home: Path,
    tmp_path: Path,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_runtime: CommandRuntime,
) -> None:
    step = next(
        candidate
        for candidate in converge_host.CONVERGE_STEPS
        if candidate.apply is external_skills.converge_external_agent_skill
    )
    assert step.host_global
    assert not step.requires_unit_config
    assert step.roles == ALL_ROLES
    (home / ".codex").mkdir()

    dev_repo = tmp_path / ".worktrees" / "feature"
    _write_source(dev_repo)
    converge_host.run_converge(
        _converge_ctx(
            dev_repo,
            home,
            operator_database=operator_database,
            producer=operator_pipeline,
            command_runtime=command_runtime,
        ),
        (step,),
        prod_install=False,
    )
    assert not _target(home / ".codex").exists()

    prod_repo = tmp_path / "prod" / "source"
    _write_source(prod_repo)
    (home / ".meiva" / "configs").mkdir(parents=True)
    converge_host.run_converge(
        _converge_ctx(
            prod_repo,
            home,
            operator_database=operator_database,
            producer=operator_pipeline,
            command_runtime=command_runtime,
        ),
        (step,),
        prod_install=True,
    )
    assert _target(home / ".codex").is_dir()

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from base.telemetry import EventPipeline
from cli.commands.converge import incomplete
from cli.commands.lifecycle import converge_host
from cli.commands.lifecycle.status import print_converge_section
from cli.database.tests.command_inputs import command_runtime as command_runtime
from tests.path_scoped.cli_tests import operator_database as operator_database
from tests.path_scoped.cli_tests import operator_pipeline as operator_pipeline

_STEP = "permissions helper build + sign + load"


def _converge(
    home: Path,
    tmp_path: Path,
    *,
    steps: tuple[converge_host.ConvergeStep, ...],
    command_runtime: Any,
    database_factory: Callable[[], Any],
    producer: Callable[[], EventPipeline],
) -> None:
    converge_host.converge_host(
        tmp_path / "repo",
        frozenset({"agent-runner"}),
        meiva_home=home,
        steps=steps,
        services=frozenset(),
        database_factory=database_factory,
        producer=producer,
        command=command_runtime,
    )


# meiva:note
# The marker is what makes an aborted converge visible after `meiva start` has
# brought the services up: the step name and its reason survive in the home.
# meiva:link cli/commands/converge/docs/converge-incomplete.meiva.okf.md
def test_the_prod_install_records_the_failing_step(tmp_path: Path) -> None:
    home = tmp_path / "home"

    converge_host.record_incomplete(home, step=_STEP, error="keychain locked", prod_install=True)

    recorded = incomplete.read_incomplete(home)
    assert recorded is not None
    assert recorded.step == _STEP
    assert "keychain locked" in recorded.error


def test_a_dev_cluster_never_writes_the_marker(tmp_path: Path) -> None:
    home = tmp_path / "home"

    converge_host.record_incomplete(home, step=_STEP, error="keychain locked", prod_install=False)

    assert incomplete.read_incomplete(home) is None


def test_an_aborted_converge_in_a_dev_cluster_leaves_no_marker(
    tmp_path: Path,
    command_runtime: Any,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
) -> None:
    home = tmp_path / "home"

    def locked(_ctx: converge_host.ConvergeCtx) -> None:
        raise RuntimeError("the login keychain is not unlocked")

    with pytest.raises(RuntimeError):
        _converge(
            home,
            tmp_path,
            steps=(converge_host.ConvergeStep(_STEP, locked),),
            command_runtime=command_runtime,
            database_factory=operator_database,
            producer=operator_pipeline,
        )

    assert incomplete.read_incomplete(home) is None


def test_a_dev_cluster_converge_leaves_the_prod_marker_alone(
    tmp_path: Path,
    command_runtime: Any,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
) -> None:
    home = tmp_path / "home"
    incomplete.record_incomplete(home, step=_STEP, error="locked")

    _converge(
        home,
        tmp_path,
        steps=(converge_host.ConvergeStep("noop", lambda _ctx: None),),
        command_runtime=command_runtime,
        database_factory=operator_database,
        producer=operator_pipeline,
    )

    assert incomplete.read_incomplete(home) is not None


def test_status_prints_nothing_when_the_home_converged(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    print_converge_section(tmp_path)

    assert capsys.readouterr().out == ""


def test_status_prints_the_incomplete_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    incomplete.record_incomplete(tmp_path, step=_STEP, error="the login keychain is not unlocked")

    print_converge_section(tmp_path)

    out = capsys.readouterr().out
    assert out.startswith("converge: INCOMPLETE ")
    assert _STEP in out and "keychain is not unlocked" in out

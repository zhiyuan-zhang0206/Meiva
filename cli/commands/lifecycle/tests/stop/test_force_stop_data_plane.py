# meiva:note
# A force stop's data-plane step against a real Redis: its return code is the stop's verdict.
#
# The stop runs the whole shell as the operator would (`do_stop` with `force`), with a real
# password-protected `redis-server` as the cluster's own instance and the real `redis-cli`
# the data plane drives. What the step's return code means is `force_outcome`'s
# (`test_force_outcome.py`).

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from base.telemetry import EventPipeline
from cli.commands.lifecycle.stop import do_stop
from cli.commands.lifecycle.tests.stop.stop_support import (
    cluster_redis,
    gateway_with_redis,
    redis_answers,
    stop_environment,
    stop_request,
)
from cli.commands.lifecycle.tests.stop.stop_support import home as home
from cli.commands.lifecycle.tests.stop.stop_support import stop_runtime as stop_runtime
from cli.database import OperatorDatabaseFactory
from cli.database.runtime import CommandRuntime
from cli.database.tests.cli_tests import operator_database as operator_database
from cli.database.tests.cli_tests import operator_pipeline as operator_pipeline
from cli.database.tests.command_inputs import command_runtime as command_runtime


def test_a_force_stop_whose_redis_shut_down_succeeds(
    redis_access: None,
    home: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    operator_database: OperatorDatabaseFactory,
    operator_pipeline: Callable[[], EventPipeline],
    stop_runtime: CommandRuntime,
) -> None:
    with cluster_redis(tmp_path / "redis", refuse_shutdown=False) as port:
        runtime = gateway_with_redis(stop_runtime, tmp_path / "redis-tools", port)

        rc = do_stop(
            stop_request(force=True, keep_infra=False, timeout=15.0),
            stop_environment(runtime, operator_database, operator_pipeline),
            retained_children=[],
        )

        assert rc == 0
        assert not redis_answers(port)
    assert "redis stopped" in capsys.readouterr().out


# meiva:note
# The defect: the force path discarded the cluster instance stop's return code, so a Redis that
# kept answering after its shutdown still ended `meiva stop --mode force` with exit code 0.
def test_a_force_stop_whose_redis_keeps_answering_fails_and_says_why(
    redis_access: None,
    home: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    operator_database: OperatorDatabaseFactory,
    operator_pipeline: Callable[[], EventPipeline],
    stop_runtime: CommandRuntime,
) -> None:
    with cluster_redis(tmp_path / "redis", refuse_shutdown=True) as port:
        runtime = gateway_with_redis(stop_runtime, tmp_path / "redis-tools", port)

        rc = do_stop(
            stop_request(force=True, keep_infra=False, timeout=15.0),
            stop_environment(runtime, operator_database, operator_pipeline),
            retained_children=[],
        )

        assert rc == 1
        assert redis_answers(port), "the instance was left running, as reported"
    err = capsys.readouterr().err
    assert f"redis still answers on 127.0.0.1:{port} after shutdown" in err
    assert "Force stop incomplete: the data plane did not stop (exit code 1)" in err

# meiva:note
# The shell of `meiva restart`: what it does with a refusal and with the lifecycle journal.
#
# Which legs run, in which order, and what each result means is `restart_plan`'s
# (`test_restart_plan.py`); the legs themselves are the start and preflight owners'. Here the
# restart meets the real journal and the real hosting evidence of its process.

from __future__ import annotations

import json
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

import psutil
import pytest

from base.deploy.lifecycle import status_journal
from base.host.exit_codes import RESTART_DECLINED_EXIT_CODE
from base.native_process.kernel_identity import pid_starttime_ticks
from base.sessions.record import SessionRecord
from base.telemetry import EventPipeline
from cli.commands.lifecycle.stop.restart import cmd_restart, cmd_restart_agent_host, conclude
from cli.commands.lifecycle.stop.restart_plan import Finish
from cli.commands.lifecycle.tests.runtime_at import runtime_at
from cli.commands.lifecycle.tests.stop.stop_support import home as home
from cli.commands.lifecycle.tests.stop.stop_support import stop_runtime as stop_runtime
from cli.database import OperatorDatabaseFactory
from cli.database.runtime import CommandRuntime
from cli.database.tests.cli_tests import operator_database as operator_database
from cli.database.tests.cli_tests import operator_pipeline as operator_pipeline
from cli.database.tests.command_inputs import command_runtime as command_runtime


def restart(
    runtime: CommandRuntime,
    database_factory: OperatorDatabaseFactory,
    producer: Callable[[], EventPipeline],
    *,
    retained_children: list[subprocess.Popen[bytes]],
) -> int:
    return cmd_restart(
        only_services=(),
        disabled_services=(),
        all_services=True,
        keep_shell_sessions=False,
        keep_browser=True,
        restart_extras=False,
        force=False,
        timeout=30.0,
        start_timeout=30.0,
        runtime=runtime_at(runtime.home),
        retained_children=retained_children,
        database_factory=database_factory,
        producer=producer,
        command=runtime,
    )


# meiva:note
# Record this process as the one a supervised session hosts (the restart would sever it).
def host_this_process_in_a_supervised_session(home: Path) -> None:
    me = psutil.Process()
    SessionRecord(
        me.pid,
        me.create_time(),
        "private-test",
        str(home),
        time.time(),
        pid_starttime_ticks(me.pid),
    ).write(home / "run/sessions/hosting-the-test.json")


# meiva:note
# Its stop leg kills every service session's tree, this restart with it: refused before
# the journal opens, with the decline exit code the updater reads as 'host still serving'.
def test_a_restart_hosted_by_a_supervised_session_is_declined_and_touches_nothing(
    home: Path,
    capsys: pytest.CaptureFixture[str],
    operator_database: OperatorDatabaseFactory,
    operator_pipeline: Callable[[], EventPipeline],
    stop_runtime: CommandRuntime,
) -> None:
    host_this_process_in_a_supervised_session(home)
    rc = restart(stop_runtime, operator_database, operator_pipeline, retained_children=[])
    assert rc == RESTART_DECLINED_EXIT_CODE
    err = capsys.readouterr().err
    assert "refusing restart" in err and "'hosting-the-test'" in err
    assert not (home / "run/lifecycle-op.json").exists(), "no journal was opened"


def test_restarting_agent_host_inside_a_supervised_session_is_incomplete_not_a_traceback(
    home: Path,
    capsys: pytest.CaptureFixture[str],
    operator_database: OperatorDatabaseFactory,
    stop_runtime: CommandRuntime,
) -> None:
    host_this_process_in_a_supervised_session(home)
    rc = cmd_restart_agent_host(
        force=False,
        timeout=5.0,
        start_timeout=5.0,
        database_factory=operator_database,
        command=stop_runtime,
    )
    assert rc == 1
    assert "restart of agent-host incomplete: stop must run outside the work it drains" in (
        capsys.readouterr().err
    )


# meiva:note
# An outer operation's still-running journal is never closed by the nested restart: the
# journal's opener closes it (task #2898).
def test_the_restart_finishes_the_journal_only_when_it_owns_it(home: Path) -> None:
    assert status_journal.begin(home, "update"), "the outer operation opens the journal"
    assert not status_journal.begin(home, "restart"), "the nested restart is not its owner"

    assert conclude(home, Finish(rc=0, error=None, notice=None), owns_journal=False) == 0
    journal = json.loads((home / "run/lifecycle-op.json").read_text())
    assert not journal["complete"], "the outer operation still owns the journal"

    status_journal.finish(home, 0)
    assert status_journal.begin(home, "restart"), (
        "with no outer operation the restart opens its own"
    )
    assert (
        conclude(home, Finish(rc=1, error="stop leg failed", notice=None), owns_journal=True) == 1
    )
    journal = json.loads((home / "run/lifecycle-op.json").read_text())
    assert journal["complete"] and journal["result"]["rc"] == 1
    assert journal["result"]["error"] == "stop leg failed"


def test_a_leg_refusal_is_said_on_stderr_once_and_returns_its_code(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    outcome = Finish(rc=RESTART_DECLINED_EXIT_CODE, error="preflight probes failed", notice="no")
    assert conclude(home, outcome, owns_journal=False) == RESTART_DECLINED_EXIT_CODE
    assert capsys.readouterr().err == "no\n"


# meiva:note
# A home with no persisted setup cannot register or probe: the restart is declined before
# anything stops, the journal is closed with the reason, and the host is still serving.
def test_a_restart_whose_probes_fail_is_declined_and_stops_nothing(
    home: Path,
    capsys: pytest.CaptureFixture[str],
    operator_database: OperatorDatabaseFactory,
    operator_pipeline: Callable[[], EventPipeline],
    stop_runtime: CommandRuntime,
) -> None:
    rc = restart(stop_runtime, operator_database, operator_pipeline, retained_children=[])
    assert rc == RESTART_DECLINED_EXIT_CODE
    err = capsys.readouterr().err
    assert "refusing restart: preflight probes failed — host still serving" in err
    journal = json.loads((home / "run/lifecycle-op.json").read_text())
    assert journal["complete"] and journal["result"]["rc"] == RESTART_DECLINED_EXIT_CODE
    assert journal["result"]["error"] == "preflight probes failed"
    assert [phase["name"] for phase in journal["phases"]] == ["preflight"]

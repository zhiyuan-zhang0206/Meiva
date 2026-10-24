# meiva:note
# Operator and updater stop semantics, with private real process boundaries.
#
# The plan decides what a stop does (`test_stop_plan.py`); these tests run the shell against the
# real thing: a pty-sessions service with real shells, the database a stop writes its notices
# into, the process table, the lifecycle journal.

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from threading import Event, Thread
from typing import NoReturn

import psutil
import psycopg
import pytest

from base.deploy.lifecycle import home_lifecycle_locks, start_serving
from base.host.system.tests.fake_commands import DARWIN_ARM64, fake_host
from base.native_process import pid_starttime_ticks
from base.sessions.pty import client
from base.sessions.pty.tests import pty_jobs as jobs
from base.sessions.pty.tests.pty_reaper import PtyReaper
from base.sessions.pty.tests.pty_service import service_socket
from base.sessions.pty.tests.pty_shells import new
from base.sessions.record import SessionRecord
from base.telemetry import EventPipeline
from cli.commands.lifecycle._exclusive_resources import exclusive_resources
from cli.commands.lifecycle.stop import browser, do_stop, refuse_hosted_stop
from cli.commands.lifecycle.stop.extras import stop_extras
from cli.commands.lifecycle.stop.restart_plan import RestartRequest, restart_stop_request
from cli.commands.lifecycle.tests.stop.stop_support import (
    Launcher,
    PtyServiceProcess,
    busy_session,
    stop_environment,
    stop_request,
    written_notices,
)
from cli.commands.lifecycle.tests.stop.stop_support import home as home
from cli.commands.lifecycle.tests.stop.stop_support import launch as launch
from cli.commands.lifecycle.tests.stop.stop_support import pty_service as pty_service
from cli.commands.lifecycle.tests.stop.stop_support import stop_runtime as stop_runtime
from cli.database import OperatorDatabaseFactory
from cli.database.runtime import CommandRuntime
from cli.database.tests.cli_tests import operator_database as operator_database
from cli.database.tests.cli_tests import operator_pipeline as operator_pipeline
from cli.database.tests.command_inputs import command_runtime as command_runtime

RESTART = RestartRequest(
    only_services=(),
    disabled_services=(),
    all_services=True,
    keep_shell_sessions=False,
    keep_browser=True,
    restart_extras=False,
    force=False,
    timeout=30.0,
    start_timeout=30.0,
)


def test_keeping_shell_sessions_preserves_unselected_process_and_real_pty(
    home: Path,
    launch: Launcher,
    pty_reaper: PtyReaper,
    pty_service: PtyServiceProcess,
    operator_database: OperatorDatabaseFactory,
    operator_pipeline: Callable[[], EventPipeline],
    stop_runtime: CommandRuntime,
) -> None:
    # meiva:note
    # `--keep-shell-sessions` leaves the pty-sessions service and its live session as they are,
    # and a process the stop does not own is never touched.
    orchestration = launch("unowned-test-process", term="ignore")
    name = "meiva-agent-987-shell-1"
    assert new(name, home, {"MEIVA_HOME": str(home)})
    identity = pty_reaper.track_session(name)
    deadline = time.monotonic() + 5
    while psutil.Process(identity.pid).children(recursive=True):
        assert time.monotonic() < deadline
        time.sleep(0.05)
    rc = do_stop(
        stop_request(keep_shell_sessions=True, timeout=5),
        stop_environment(stop_runtime, operator_database, operator_pipeline),
        retained_children=[],
    )
    assert rc == 0
    assert orchestration.poll() is None
    assert client.has_session(name, socket_path=service_socket()) and identity.live()
    assert pty_service.process is not None and pty_service.process.poll() is None


# meiva:note
# `meiva restart` stops and restarts the application services and closes the persistent
# shells as `meiva stop` does: a busy session's owner gets its notice.
def test_the_restart_stop_leg_closes_the_shells_and_tells_a_busy_sessions_owner(
    redis_url: str,
    home: Path,
    launch: Launcher,
    pty_reaper: PtyReaper,
    pty_service: PtyServiceProcess,
    db_conn: psycopg.Connection,
    operator_database: OperatorDatabaseFactory,
    operator_pipeline: Callable[[], EventPipeline],
    stop_runtime: CommandRuntime,
) -> None:
    unowned = launch("unowned-test-process", term="ignore")
    name = "meiva-agent-987-shell-2056-smooth"
    busy_session(home, name, jobs.TERM_OK, pty_reaper)
    request = restart_stop_request(RESTART, roles=stop_runtime.machine.role())
    rc = do_stop(
        request,
        stop_environment(stop_runtime, operator_database, operator_pipeline),
        retained_children=[],
    )
    assert rc == 0
    assert not client.has_session(name, socket_path=service_socket()), (
        "a restart closes the persistent shells"
    )
    assert [notice["name"] for notice in written_notices(db_conn)] == [name], (
        "the busy session's owner is told"
    )
    assert unowned.poll() is None


def test_a_full_stop_closes_a_real_idle_shell_session(
    home: Path,
    pty_reaper: PtyReaper,
    pty_service: PtyServiceProcess,
    operator_database: OperatorDatabaseFactory,
    operator_pipeline: Callable[[], EventPipeline],
    stop_runtime: CommandRuntime,
) -> None:
    name = "meiva-agent-987-shell-2"
    assert new(name, home, {"MEIVA_HOME": str(home)})
    pty_reaper.track_session(name)
    rc = do_stop(
        stop_request(timeout=5),
        stop_environment(stop_runtime, operator_database, operator_pipeline),
        retained_children=[],
    )
    assert rc == 0
    assert not client.has_session(name, socket_path=service_socket())
    assert client.list_sessions(socket_path=service_socket()) == []


# meiva:note
# A stop that closed no busy session writes no notice and so never dials the database.
def test_repeated_stop_needs_no_live_database(
    home: Path,
    operator_pipeline: Callable[[], EventPipeline],
    stop_runtime: CommandRuntime,
) -> None:
    def down() -> NoReturn:
        raise AssertionError("DB is down: a stop with no notice to write must not reach for it")

    rc = do_stop(
        stop_request(timeout=1),
        stop_environment(stop_runtime, down, operator_pipeline),
        retained_children=[],
    )
    assert rc == 0


def test_resource_stop_excludes_concurrent_start(home: Path) -> None:
    entered, finish, start_finished = Event(), Event(), Event()
    order: list[str] = []

    @exclusive_resources
    def stopping() -> None:
        order.append("stop")
        entered.set()
        assert finish.wait(5)

    @exclusive_resources
    def starting() -> int:
        order.append("start")
        return 0

    thread = Thread(target=stopping)
    thread.start()
    assert entered.wait(5)

    def waiting_start() -> None:
        assert starting() == 0
        start_finished.set()

    starter = Thread(target=waiting_start)
    starter.start()
    try:
        assert not start_finished.wait(0.2)
        assert order == ["stop"], "the start waits for the stop to release the lock"
    finally:
        finish.set()
        thread.join(5)
        starter.join(5)
    assert not thread.is_alive() and not starter.is_alive()
    assert start_finished.is_set() and order == ["stop", "start"]
    # meiva:note
    # The lock is free again once both are done.
    with home_lifecycle_locks.resource_lock(purpose="test:after", timeout_s=1):
        pass


@pytest.mark.parametrize("how", ["stop", "restart", "keep-shell-sessions"])
def test_explicit_force_stops_host_and_closes_shell_sessions_unless_kept(
    how: str,
    redis_url: str,
    home: Path,
    pty_reaper: PtyReaper,
    pty_service: PtyServiceProcess,
    db_conn: psycopg.Connection,
    operator_database: OperatorDatabaseFactory,
    operator_pipeline: Callable[[], EventPipeline],
    stop_runtime: CommandRuntime,
) -> None:
    # meiva:note
    # Force stops the services with the pty-sessions service kept, then closes the shell
    # sessions through it and stops it, with no drain and no owner notices; keeping the shell
    # sessions keeps the live session.
    name = "meiva-agent-987-shell-2057-force"
    busy_session(home, name, jobs.TERM_OK, pty_reaper)
    kept = how == "keep-shell-sessions"
    request = (
        restart_stop_request(
            replace(RESTART, force=True, keep_shell_sessions=kept),
            roles=stop_runtime.machine.role(),
        )
        if how != "stop"
        else stop_request(force=True, timeout=15.0)
    )
    rc = do_stop(
        request,
        stop_environment(stop_runtime, operator_database, operator_pipeline),
        retained_children=[],
    )
    assert rc == 0
    assert client.has_session(name, socket_path=service_socket()) is kept
    assert written_notices(db_conn) == [], "force writes no owner notices"


# meiva:note
# The 2026-09-12 shape: the services phase ran and the data-plane phase failed. The stop
# fails loudly and leaves the decision to the operator: no `meiva start` child is launched
# and the journal carries no restore verdict.
def test_stop_reports_a_data_plane_failure_without_restarting_anything(
    home: Path,
    capsys: pytest.CaptureFixture[str],
    operator_database: OperatorDatabaseFactory,
    operator_pipeline: Callable[[], EventPipeline],
    stop_runtime: CommandRuntime,
) -> None:
    stop_runtime.machine.set(role="gateway")
    # meiva:note
    # A remote-managed data plane is one a local stop cannot verify: the data-plane phase fails.
    stop_runtime.config.set_field("db_url", "postgresql://u:p@192.0.2.4:5432/meiva")
    stop_runtime.config.set_field("redis_url", "redis://:p@192.0.2.4:6379")
    rc = do_stop(
        stop_request(keep_infra=False, timeout=5),
        stop_environment(stop_runtime, operator_database, operator_pipeline),
        retained_children=[],
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "Stop incomplete" in err and "remote-managed" in err
    assert "phases: services" in err and "data-plane" in err
    assert (
        "Retry the command, or use meiva start (with this unit's explicit service selection) "
        "to resume." in err
    )
    assert "compensating" not in err
    journal = json.loads((home / "run/lifecycle-op.json").read_text())
    assert journal["complete"] and journal["result"]["rc"] == 1
    assert "compensated" not in journal["result"]


# meiva:note
# A stop closes this unit's persistent shell sessions and kills the supervised ones: run
# from inside one, it would strand itself mid-drain (issue #2331).
def test_a_stop_run_from_inside_a_supervised_session_is_refused_before_it_stops_anything(
    home: Path,
    operator_database: OperatorDatabaseFactory,
    operator_pipeline: Callable[[], EventPipeline],
    stop_runtime: CommandRuntime,
) -> None:
    me = psutil.Process()
    SessionRecord(
        me.pid,
        me.create_time(),
        "private-test",
        str(home),
        time.time(),
        pid_starttime_ticks(me.pid),
    ).write(home / "run/sessions/hosting-the-test.json")

    with pytest.raises(RuntimeError, match="login shell"):
        refuse_hosted_stop(home)
    with pytest.raises(RuntimeError, match="login shell"):
        do_stop(
            stop_request(timeout=1),
            stop_environment(stop_runtime, operator_database, operator_pipeline),
            retained_children=[],
        )
    # meiva:note
    # An explicit force is the operator's own interruption and was never refused for this.
    forced = do_stop(
        stop_request(force=True, timeout=1),
        stop_environment(stop_runtime, operator_database, operator_pipeline),
        retained_children=[],
    )
    assert forced == 0


def test_retiring_extras_on_a_host_without_a_helper_retires_nothing(
    stop_runtime: CommandRuntime,
) -> None:
    deadline = time.monotonic() + 30.0
    stop_extras(stop_runtime, deadline, retained=frozenset())
    with pytest.raises(ValueError, match="retains browser"):
        stop_extras(stop_runtime, deadline, retained=frozenset({"browser"}))


# meiva:note
# On a macOS host the stop hands this home's permissions helper to the launchd owner, asking
# about the port the configuration registered, and what that owner refuses is the stop's failure.
# A port no helper can be registered on is refused before any launchd job is queried.
@pytest.mark.requires_host("darwin")
def test_a_macos_stop_asks_the_helper_owner_about_its_configured_port_and_lets_a_refusal_through(
    stop_runtime: CommandRuntime,
) -> None:
    macos = replace(stop_runtime, host=fake_host(platform=DARWIN_ARM64))
    macos.config.set_field("permissions_helper_port", 0)

    with pytest.raises(ValueError, match="canonical home and registered port"):
        stop_extras(macos, time.monotonic() + 30.0, retained=frozenset())


# meiva:note
# A deliberate stop revokes the prior start's recovery authority before any daemon has a
# chance to observe its own shutdown or a dead peer.
def test_a_stop_revokes_the_serving_authority_of_the_prior_start(
    home: Path,
    operator_database: OperatorDatabaseFactory,
    operator_pipeline: Callable[[], EventPipeline],
    stop_runtime: CommandRuntime,
) -> None:
    start_serving.begin_start(home)
    assert start_serving.state_path(home).exists()
    for force in (False, True):
        start_serving.begin_start(home)
        rc = do_stop(
            stop_request(force=force, timeout=5),
            stop_environment(stop_runtime, operator_database, operator_pipeline),
            retained_children=[],
        )
        assert rc == 0
        assert not start_serving.state_path(home).exists(), f"force={force}"


# meiva:note
# With no Chrome on this cluster's profile, the wait returns, the sweep says nothing, and
# the recording has no browser to ask (a decorative step: never a reason to stop failing).
def test_a_browser_that_is_not_there_costs_a_stop_nothing(
    home: Path,
    capsys: pytest.CaptureFixture[str],
    stop_runtime: CommandRuntime,
) -> None:
    browser.stop_browser(time.monotonic() + 5)
    browser.reap_cluster_chrome()
    assert capsys.readouterr().out == ""
    browser.record_browser_session(stop_runtime)
    assert "nothing to record (no browser answered)" in capsys.readouterr().out

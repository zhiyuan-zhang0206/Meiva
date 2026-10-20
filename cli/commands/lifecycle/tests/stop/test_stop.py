"""Operator and updater stop semantics, with private real process boundaries."""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock

import psutil
import pytest

import cli.commands._repo as _repo_commands
import cli.commands.lifecycle.root_driver as _root_driver_commands
from base.agents.context.clients import DatabaseFactory
from base.cluster.machine import MachineIdentity
from base.db import Database
from base.deploy.lifecycle import home_lifecycle_locks
from base.host import proc
from base.sessions.pty import client
from base.sessions.pty.paths import SERVICE_UNIT
from base.sessions.pty.tests import pty_jobs as jobs
from base.sessions.pty.tests.pty_reaper import PtyReaper
from base.sessions.pty.tests.pty_shells import new
from base.sessions.tests.refuse_helper import refuse_helper
from base.telemetry import EventPipeline
from cli.commands.lifecycle import _stop_extras
from cli.commands.lifecycle import _temporary_stop as command
from cli.commands.lifecycle import stop as entry
from cli.commands.lifecycle._exclusive_resources import StartDelegation, delegable_start
from cli.commands.lifecycle.tests.stop.stop_support import (
    Launcher,
    PtyServiceProcess,
    busy_session,
    dependencies,
    record_root_stops,
)
from cli.commands.lifecycle.tests.stop.stop_support import home as home
from cli.commands.lifecycle.tests.stop.stop_support import launch as launch
from cli.commands.lifecycle.tests.stop.stop_support import pty_service as pty_service
from cli.commands.lifecycle.tests.stop.stop_support import written as written
from cli.database import OperatorDatabaseFactory
from cli.database.runtime import CommandRuntime
from cli.database.tests.command_inputs import command_runtime as command_runtime
from ops import pty_close_notices
from tests.path_scoped.cli_tests import operator_database as operator_database
from tests.path_scoped.cli_tests import operator_pipeline as operator_pipeline


def _restart_stop(
    *,
    database_factory: DatabaseFactory,
    producer: Callable[[], EventPipeline],
    command_runtime: CommandRuntime,
    timeout: float,
    force: bool = False,
    keep_shell_sessions: bool = False,
) -> int:
    # meiva:note
    # The stop leg of `meiva restart`: no prompt; the data plane (when this host has
    # one) and the browser stay, and no extras are torn down.
    machine = command_runtime.machine
    return entry.do_stop(
        Path("/unused"),
        require_confirmation=False,
        keep_infra=True if "gateway" in machine.role() else None,
        keep_browser=True,
        keep_shell_sessions=keep_shell_sessions,
        preserve_sessions=frozenset(),
        announce=False,
        teardown_extras=False,
        force=force,
        timeout=timeout,
        database_factory=database_factory,
        producer=producer,
        machine=command_runtime.machine,
        helper=refuse_helper,
    )


def test_keeping_shell_sessions_preserves_unselected_process_and_real_pty(
    home: Path,
    launch: Launcher,
    monkeypatch: pytest.MonkeyPatch,
    pty_reaper: PtyReaper,
    pty_service: PtyServiceProcess,
    operator_database: OperatorDatabaseFactory,
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_runtime: CommandRuntime,
) -> None:
    # meiva:note
    # `--keep-shell-sessions` leaves the pty-sessions service and its live session as they
    # are: the root owner is asked to preserve it, and no shell-sessions phase runs.
    dependencies(monkeypatch, machine=command_runtime.machine)
    root_stops = record_root_stops(monkeypatch)

    def closed(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("a kept service's shell sessions must not be closed")

    monkeypatch.setattr(command, "close_shell_sessions", closed)
    # meiva:note
    # Bootstrap and spawned interpreters consume the raw home before Settings.
    monkeypatch.setitem(os.environ, "MEIVA_HOME", str(home))
    monkeypatch.setenv("HOME", str(home))
    orchestration = launch("unowned-test-process", term="ignore")
    name = "meiva-agent-987-shell-1"
    assert new(name, home, {"MEIVA_HOME": str(home)})
    identity = pty_reaper.track_session(name)
    deadline = time.monotonic() + 5
    while psutil.Process(identity.pid).children(recursive=True):
        assert time.monotonic() < deadline
        time.sleep(0.05)
    assert (
        _restart_stop(
            keep_shell_sessions=True,
            timeout=5,
            database_factory=operator_database,
            producer=operator_pipeline,
            command_runtime=command_runtime,
        )
        == 0
    )
    assert orchestration.poll() is None
    assert client.has_session(name) and identity.live()
    assert pty_service.process is not None and pty_service.process.poll() is None
    assert [call["preserve"] for call in root_stops] == [{"browser", SERVICE_UNIT}]


def test_smooth_restart_replaces_services_and_closes_shells_but_keeps_data_plane(
    home: Path,
    launch: Launcher,
    monkeypatch: pytest.MonkeyPatch,
    pty_reaper: PtyReaper,
    pty_service: PtyServiceProcess,
    written: list[pty_close_notices.ClosureNotice],
    operator_database: OperatorDatabaseFactory,
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_runtime: CommandRuntime,
) -> None:
    """`meiva restart` (smooth) stops and restarts the application services, closes the
    persistent shells as `meiva stop` does (a busy session's owner gets its notice) and
    leaves the browser, the permissions helper and the data plane alone, on a gateway
    host where a full stop would take each of them down."""
    import cli.commands.lifecycle.start as start_commands
    import cli.commands.lifecycle.stop as stop_commands
    from cli.commands.lifecycle import _start_readiness_preflight, service_stop

    dependencies(monkeypatch, machine=command_runtime.machine)
    monkeypatch.setitem(os.environ, "MEIVA_HOME", str(home))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(command_runtime.machine, "role", lambda: frozenset({"gateway"}))
    unowned = launch("unowned-test-process", term="ignore")
    name = "meiva-agent-987-shell-2056-smooth"
    busy_session(home, name, jobs.TERM_OK, pty_reaper)

    events: list[str] = []

    def record_root_stop(**kwargs: object) -> None:
        # meiva:note
        # The browser is retained from the service tree and the services are replaced; the
        # pty-sessions service stays until the shell sessions it holds are closed, then goes too.
        assert kwargs.get("force", False) is False
        preserve = cast("frozenset[str]", kwargs["preserve"])
        assert preserve - {SERVICE_UNIT} == frozenset({"browser"})
        events.append(
            "services-stopped" if SERVICE_UNIT in preserve else "shell-session-service-stopped"
        )

    def record(label: str) -> Callable[..., object]:
        return lambda *_args, **_kwargs: events.append(label)

    def record_start(_database_factory: DatabaseFactory, **kwargs: object) -> int:
        assert (kwargs["only_services"], kwargs["disabled_services"]) == ((), ())
        assert (kwargs["all_services"], kwargs["reconcile"]) == (True, False)
        events.append("services-started")
        return 0

    real_close = service_stop.close_shell_sessions

    def close_shell_sessions(*args: Any, **kwargs: Any) -> None:
        events.append("shell-sessions-closed")
        real_close(*args, **kwargs)

    monkeypatch.setattr(_root_driver_commands, "stop_root_service_tree", record_root_stop)
    monkeypatch.setattr(command, "stop_data_plane", record("data-plane-stopped"))
    monkeypatch.setattr(command, "stop_browser", record("browser-stopped"))
    monkeypatch.setattr(command, "stop_extras", record("extras-stopped"))
    monkeypatch.setattr(command, "close_shell_sessions", close_shell_sessions)
    monkeypatch.setattr(command, "announce_stopping", record("announced"))
    monkeypatch.setattr(stop_commands, "restart_runtime", MagicMock)
    monkeypatch.setattr(stop_commands, "require_restart_runtime", lambda _runtime: None)  # pyright: ignore[reportUnknownArgumentType]

    def preflight(_db: object, *, command: CommandRuntime) -> int:
        assert command is command_runtime
        del _db
        return 0

    monkeypatch.setattr(_repo_commands, "preflight_probes", preflight)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(
        _start_readiness_preflight,
        "preflight_start_readiness",
        lambda *_a, **_k: 0,  # pyright: ignore[reportUnknownArgumentType]
    )
    monkeypatch.setattr(start_commands, "cmd_start_body", record_start)

    assert (
        entry.cmd_restart(
            only_services=(),
            disabled_services=(),
            all_services=True,
            keep_shell_sessions=False,
            keep_browser=True,
            restart_extras=False,
            force=False,
            timeout=30.0,
            start_timeout=30.0,
            retained_children=[],
            database_factory=operator_database,
            producer=operator_pipeline,
            command=command_runtime,
        )
        == 0
    )

    assert events == [
        "services-stopped",
        "shell-sessions-closed",
        "shell-session-service-stopped",
        "services-started",
    ]
    assert not client.has_session(name), "a restart closes the persistent shells"
    assert [notice.name for notice in written] == [name], "the busy session's owner is told"
    assert unowned.poll() is None


def test_root_stop_refusal_is_reported_without_force(
    home: Path,
    launch: Launcher,
    monkeypatch: pytest.MonkeyPatch,
    operator_database: OperatorDatabaseFactory,
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_runtime: CommandRuntime,
) -> None:
    dependencies(monkeypatch, machine=command_runtime.machine)
    service = launch("meiva-worker", term="ignore")
    before = psutil.Process(service.pid).create_time()

    def refuse(**kwargs: object) -> None:
        assert kwargs.get("force", False) is False
        raise RuntimeError("root service did not stop")

    monkeypatch.setattr(_root_driver_commands, "stop_root_service_tree", refuse)
    assert (
        _restart_stop(
            timeout=0.2,
            database_factory=operator_database,
            producer=operator_pipeline,
            command_runtime=command_runtime,
        )
        == 1
    )
    assert service.poll() is None
    assert psutil.Process(service.pid).create_time() == before


# meiva:note
# The services phase keeps the pty-sessions service; the shell-sessions phase closes
# the session through it; only then is the service itself stopped.
def test_full_stop_closes_real_idle_shell_session_before_the_service_stops(
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
    pty_reaper: PtyReaper,
    pty_service: PtyServiceProcess,
    operator_database: OperatorDatabaseFactory,
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_runtime: CommandRuntime,
) -> None:
    dependencies(monkeypatch, machine=command_runtime.machine)
    monkeypatch.setitem(os.environ, "MEIVA_HOME", str(home))
    monkeypatch.setenv("HOME", str(home))
    root_stops: list[tuple[bool, list[str]]] = []

    # meiva:note
    # Whether the service was asked to stay, and the sessions it held at that moment.
    def record(**kwargs: Any) -> int:
        root_stops.append(
            (SERVICE_UNIT in kwargs["preserve"], [info.name for info in client.list_sessions()])
        )
        return 0

    monkeypatch.setattr(_root_driver_commands, "stop_root_service_tree", record)

    def no_helper(**_kw: object) -> None:
        return None

    for hook in ("stop_permissions_helper",):
        monkeypatch.setattr(_stop_extras, hook, no_helper)

    def announce(*, machine: MachineIdentity, **window: object) -> None:
        # meiva:note
        # An ordinary stop declares no maintenance window.
        assert window == {"maintenance_for_seconds": None, "maintenance_reason": None}
        assert machine is command_runtime.machine

    monkeypatch.setattr(command, "announce_stopping", announce)
    name = "meiva-agent-987-shell-2"
    assert new(name, home, {"MEIVA_HOME": str(home)})
    pty_reaper.track_session(name)
    assert (
        entry.cmd_stop(
            require_confirmation=False,
            keep_infra=None,
            keep_browser=True,
            keep_shell_sessions=False,
            teardown_extras=False,
            preserve_sessions=frozenset(),
            force=False,
            timeout=5,
            database_factory=operator_database,
            producer=operator_pipeline,
            machine=command_runtime.machine,
            helper=refuse_helper,
        )
        == 0
    )
    assert not client.has_session(name)
    assert root_stops == [(True, [name]), (False, [])], (
        "the service stops only after its shell sessions closed"
    )


def test_a_delegated_start_runs_its_child_after_the_start_lock_is_released(
    home: Path, operator_database: OperatorDatabaseFactory
) -> None:
    ran: list[bool] = []

    def child() -> int:
        # meiva:note
        # The child takes the lock the start released; a held lock would time out here.
        with home_lifecycle_locks.resource_lock(purpose="test:delegated-child", timeout_s=1):
            ran.append(True)
        return 0

    @delegable_start
    def start(_database_factory: DatabaseFactory) -> StartDelegation:
        return StartDelegation(child)

    assert start(operator_database) == 0
    assert ran == [True]


def test_only_explicit_force_enters_legacy_force_stop(
    monkeypatch: pytest.MonkeyPatch,
    operator_database: OperatorDatabaseFactory,
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_runtime: CommandRuntime,
) -> None:
    normal, force = MagicMock(return_value=0), MagicMock(return_value=0)
    monkeypatch.setattr(command, "stop", normal)
    monkeypatch.setattr(entry, "force_stop", force)
    assert (
        entry.do_stop(
            Path("/unused"),
            require_confirmation=False,
            keep_infra=None,
            keep_browser=True,
            keep_shell_sessions=False,
            preserve_sessions=frozenset(),
            announce=False,
            teardown_extras=False,
            force=False,
            timeout=5.0,
            database_factory=operator_database,
            producer=operator_pipeline,
            machine=command_runtime.machine,
            helper=refuse_helper,
        )
        == 0
    )
    normal.assert_called_once()
    force.assert_not_called()
    assert (
        entry.cmd_stop(
            keep_infra=None,
            require_confirmation=False,
            keep_browser=True,
            keep_shell_sessions=False,
            teardown_extras=False,
            preserve_sessions=frozenset(),
            force=True,
            timeout=5.0,
            database_factory=operator_database,
            producer=operator_pipeline,
            machine=command_runtime.machine,
            helper=refuse_helper,
        )
        == 0
    )
    force.assert_called_once()


def test_repeated_stop_needs_no_live_database(
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
    operator_database: OperatorDatabaseFactory,
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_runtime: CommandRuntime,
) -> None:
    dependencies(monkeypatch, machine=command_runtime.machine)
    monkeypatch.setattr(Database, "connect", MagicMock(side_effect=AssertionError("DB is down")))
    assert (
        _restart_stop(
            timeout=1,
            database_factory=operator_database,
            producer=operator_pipeline,
            command_runtime=command_runtime,
        )
        == 0
    )


def test_resource_stop_excludes_concurrent_start(
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
    operator_database: OperatorDatabaseFactory,
    command_runtime: CommandRuntime,
) -> None:
    from threading import Event, Thread

    from cli.commands.lifecycle._exclusive_resources import exclusive_resources

    entered, finish, start_finished = Event(), Event(), Event()

    @exclusive_resources
    def stopping() -> None:
        entered.set()
        assert finish.wait(5)

    thread = Thread(target=stopping)
    thread.start()
    assert entered.wait(5)
    start = MagicMock(return_value=0)

    def waiting_start() -> None:
        assert delegable_start(start)(operator_database) == 0
        start_finished.set()

    starter = Thread(target=waiting_start)
    starter.start()
    try:
        assert not start_finished.wait(0.2)
        start.assert_not_called()
    finally:
        finish.set()
        thread.join(5)
        starter.join(5)
    assert not thread.is_alive()
    assert not starter.is_alive()
    assert start_finished.is_set()
    start.assert_called_once()


@pytest.mark.parametrize("how", ["stop", "restart", "keep-shell-sessions"])
def test_explicit_force_stops_host_and_closes_shell_sessions_unless_kept(
    how: str,
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
    pty_reaper: PtyReaper,
    pty_service: PtyServiceProcess,
    written: list[pty_close_notices.ClosureNotice],
    operator_database: OperatorDatabaseFactory,
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_runtime: CommandRuntime,
) -> None:
    # meiva:note
    # Force stops the services with the pty-sessions service kept, then closes the
    # shell sessions through it and stops it, with no drain and no owner notices;
    # keeping the shell sessions keeps the live session.
    command_runtime.machine.set(role="agent-runner")
    monkeypatch.setitem(os.environ, "MEIVA_HOME", str(home))
    monkeypatch.setenv("HOME", str(home))

    def announce(*, machine: MachineIdentity, **window: object) -> None:
        # meiva:note
        # An ordinary stop declares no maintenance window.
        assert window == {"maintenance_for_seconds": None, "maintenance_reason": None}
        assert machine is command_runtime.machine

    monkeypatch.setattr(command, "announce_stopping", announce)
    for hook in ("stop_permissions_helper",):
        monkeypatch.setattr(_stop_extras, hook, lambda **_kw: None)  # pyright: ignore[reportUnknownArgumentType]
    root_calls = record_root_stops(monkeypatch)
    name = "meiva-agent-987-shell-2057-force"
    busy_session(home, name, jobs.TERM_OK, pty_reaper)
    if how == "stop":
        rc = entry.cmd_stop(
            keep_infra=None,
            require_confirmation=False,
            keep_browser=True,
            keep_shell_sessions=False,
            teardown_extras=False,
            preserve_sessions=frozenset(),
            force=True,
            timeout=15.0,
            database_factory=operator_database,
            producer=operator_pipeline,
            machine=command_runtime.machine,
            helper=refuse_helper,
        )
    elif how == "restart":
        rc = _restart_stop(
            force=True,
            timeout=15.0,
            database_factory=operator_database,
            producer=operator_pipeline,
            command_runtime=command_runtime,
        )
    else:
        rc = _restart_stop(
            force=True,
            keep_shell_sessions=True,
            timeout=15.0,
            database_factory=operator_database,
            producer=operator_pipeline,
            command_runtime=command_runtime,
        )
    kept = how == "keep-shell-sessions"
    assert rc == 0
    assert all(call["force"] is True for call in root_calls)
    assert [SERVICE_UNIT in call["preserve"] for call in root_calls] == (
        [True] if kept else [True, False]
    )
    assert client.has_session(name) is kept
    assert written == [], "force writes no owner notices"


# ── a failed data-plane stop is reported, never compensated ───────────────────


def _stop_with_gateway_data_plane(
    monkeypatch: pytest.MonkeyPatch, *, command_runtime: CommandRuntime
) -> None:
    """A gateway stop whose phases are real no-ops except what a test patches."""
    dependencies(monkeypatch, machine=command_runtime.machine)
    monkeypatch.setattr(command_runtime.machine, "role", lambda: frozenset({"gateway"}))


def test_stop_reports_a_data_plane_failure_without_restarting_anything(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    operator_database: OperatorDatabaseFactory,
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_runtime: CommandRuntime,
) -> None:
    """The 2026-09-12 shape through the real stop() flow: the services phase ran and
    the data-plane phase failed. The stop fails loudly and leaves the decision to
    the operator: no `meiva start` child is launched and the journal carries no
    restore verdict."""
    _stop_with_gateway_data_plane(monkeypatch, command_runtime=command_runtime)

    def _data_plane_stop(*_args: object, **_kw: object) -> None:
        raise RuntimeError("maintenance kept its hold; processes did not exit: [2465]")

    def _no_child(*_args: object, **_kw: object) -> None:
        raise AssertionError("a failed stop must not spawn a compensating `meiva start`")

    finished: list[dict[str, object]] = []

    def _begin(*_args: object, **_kw: object) -> bool:
        return True

    def _finish(_rc: int, *, error: str, extra: dict[str, object]) -> None:
        finished.append({"error": error, **extra})

    monkeypatch.setattr(command, "stop_data_plane", _data_plane_stop)
    monkeypatch.setattr(command.subprocess, "run", _no_child)
    monkeypatch.setattr(command, "begin", _begin)
    monkeypatch.setattr(command, "finish", _finish)

    rc = command.stop(
        require_confirmation=False,
        keep_infra=False,
        preserve_sessions=frozenset({"worker"}),
        keep_browser=True,
        keep_shell_sessions=False,
        announce=False,
        teardown_extras=False,
        timeout=1,
        database_factory=operator_database,
        producer=operator_pipeline,
        machine=command_runtime.machine,
        helper=refuse_helper,
    )

    assert rc == 1
    err = capsys.readouterr().err
    assert "Stop incomplete" in err
    assert (
        "Retry the command, or use meiva start (with this unit's explicit service selection) to resume."
        in err
    )
    assert "compensating" not in err
    assert len(finished) == 1 and "compensated" not in finished[0]


# meiva:note
# Issue #2331: an exec-domain stop is SIGKILLed mid-drain with the call's process
# group, and a stop (a restart's stop leg too) closes this unit's persistent
# shell sessions — the refusal points at a shell no meiva session hosts.
@pytest.mark.parametrize("leg", ["stop", "restart"])
def test_stop_refused_inside_an_exec_domain(
    monkeypatch: pytest.MonkeyPatch,
    leg: str,
    operator_database: OperatorDatabaseFactory,
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_runtime: CommandRuntime,
) -> None:
    dependencies(monkeypatch, machine=command_runtime.machine)
    monkeypatch.setattr(proc, "hosting_exec_domain", lambda: "agent.execution.child")

    with pytest.raises(RuntimeError, match="login shell"):
        if leg == "stop":
            entry.cmd_stop(
                keep_infra=None,
                require_confirmation=False,
                keep_browser=True,
                keep_shell_sessions=False,
                teardown_extras=False,
                preserve_sessions=frozenset(),
                force=False,
                timeout=1,
                database_factory=operator_database,
                producer=operator_pipeline,
                machine=command_runtime.machine,
                helper=refuse_helper,
            )
        else:
            _restart_stop(
                timeout=1,
                database_factory=operator_database,
                producer=operator_pipeline,
                command_runtime=command_runtime,
            )

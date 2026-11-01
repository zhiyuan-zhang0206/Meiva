# meiva:note
# The shell of a stop: perform a plan's steps one at a time, each against the real host.
#
# What to do, in what order and what to keep is `plan.plan_stop`'s answer; this module only
# executes it. A normal stop times every phase (printed before the phase runs, so a caller whose
# outer budget cuts the command off still saw which stage it entered, and recorded in the
# lifecycle journal); a force stop runs its steps untimed.

from __future__ import annotations

import subprocess
import sys
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO, assert_never

from base.agents.context.clients import DatabaseFactory
from base.agents.inbox.passive_lifecycle_note import insert_passive_host_lifecycle_notes
from base.clock import Deadline
from base.deploy.lifecycle import start_serving
from base.deploy.lifecycle.host_exit_message import (
    HOST_RESTART_SOURCE,
    clear_host_exit_message,
    consume_host_exit_message,
    write_host_exit_message,
)
from base.deploy.lifecycle.status_journal import phase as journal_phase
from base.telemetry import EventPipeline
from cli.commands.data_plane import maintenance_stop
from cli.commands.data_plane.bringup import stop_cluster_instance
from cli.commands.data_plane.config import data_plane_config_from_boot
from cli.commands.data_plane.stop_waits import remaining
from cli.commands.lifecycle import root_driver
from cli.commands.lifecycle.stop import browser, extras, shell_sessions
from cli.commands.lifecycle.stop.announce import announce_stopping
from cli.commands.lifecycle.stop.force_outcome import DataPlaneStopError, data_plane_failure
from cli.commands.lifecycle.stop.render import data_plane_skip_line
from cli.commands.lifecycle.stop.steps import (
    Announce,
    ClearServing,
    CloseShellSessions,
    ReapChrome,
    RecordBrowserSession,
    SkipDataPlane,
    StopBrowser,
    StopDataPlane,
    StopExtras,
    StopPlan,
    StopServices,
    StopStep,
    phase_label,
)
from cli.database.runtime import CommandRuntime
from services.desktop.permissions_helper.launchd_job import LaunchdFacts


# meiva:note
# Everything a stop reads from its invocation, handed down from the command entry.
#
# `stdin` is the stream the confirm gate reads its answer from (None for a stop that never asks,
# such as a restart's stop leg). The caller's registry of the processes it spawned
# (`retained_children`, kept until its final telemetry drain) is a mutable list the caller owns,
# so it travels as an argument of the stop rather than inside this frozen value.
@dataclass(frozen=True)
class StopEnvironment:
    command: CommandRuntime
    database_factory: DatabaseFactory
    producer: Callable[[], EventPipeline]
    stdin: TextIO | None
    shell_cleanup_wait_s: float = shell_sessions.SHELL_SESSION_STOP_WAIT_S


# meiva:note
# What one stop accumulates while it runs: its deadline and its per-phase accounting, which a
# failed stop reports even though the plan raised before it could return anything.
@dataclass
class StopRun:
    deadline: Deadline
    phases: list[tuple[str, float]]


# meiva:note
# What a completed stop has to say: an escalated Postgres shutdown (`notes`) and the pooler's
# clients still connected at its stop (`clients`).
@dataclass(frozen=True)
class StopOutcome:
    notes: tuple[str, ...]
    clients: tuple[str, ...]


# meiva:note
# Run one stop phase, recording its wall time even when it raises.
#
# The phase name is printed BEFORE the step runs, and the lifecycle journal carries the same
# timeline durably. An untimed step (`label` None) just runs.
@contextmanager
def timed_phase(
    home: Path, pid: int, phases: list[tuple[str, float]], label: str | None
) -> Generator[None]:
    if label is None:
        yield
        return
    started = time.monotonic()
    print(f"  · stop phase: {label}", flush=True)
    with journal_phase(home, label, pid=pid):
        try:
            yield
        finally:
            phases.append((label, time.monotonic() - started))


# meiva:note
# Perform the plan's steps in order; the first failure stops the plan.
def execute_plan(
    plan: StopPlan,
    env: StopEnvironment,
    run: StopRun,
    retained_children: list[subprocess.Popen[bytes]] | None,
    *,
    timed: bool,
) -> StopOutcome:
    outcome = StopOutcome((), ())
    for step in plan.steps:
        with timed_phase(
            env.command.home, env.command.pid, run.phases, phase_label(step) if timed else None
        ):
            reported = _perform(step, env, run, retained_children)
        if reported is not None:
            outcome = reported
    return outcome


# meiva:note
# When `agent-host` was already down before a smooth stop or restart, `GracefulExit` never ran to
# consume the staged exit message. Write the passive lifecycle notes directly so live idle agents
# on this runner still learn why the host stopped, without failing a completed stop if the DB is
# unreachable.
def flush_unconsumed_exit_notes(
    command: CommandRuntime,
    database_factory: DatabaseFactory,
    message: str,
) -> None:
    roles = command.machine.role()
    if "agent-runner" not in roles:
        return
    direct_db = "gateway" in roles
    try:
        conn = database_factory().connect(direct=direct_db)
        with conn:
            with conn.cursor() as cur:
                cur.execute("SET TRANSACTION READ WRITE")
            insert_passive_host_lifecycle_notes(
                conn,
                machine=command.machine.name(),
                source=HOST_RESTART_SOURCE,
                message=message,
            )
            conn.commit()
    except Exception as exc:
        print(
            f"lifecycle exit note for {command.machine.name()!r} could not be written: "
            f"{type(exc).__name__}: {exc}",
            file=sys.stderr,
        )


def _stop_services_step(step: StopServices, env: StopEnvironment, run: StopRun) -> None:
    command = env.command
    if step.phase == "services":
        if step.force:
            clear_host_exit_message(command.home)
        else:
            write_host_exit_message(command.home, step.message)
    root_driver.stop_root_service_tree(
        preserve=step.preserve,
        timeout_s=remaining(run.deadline),
        force=step.force,
        helper=command.helper,
        home=command.home,
        facts=LaunchdFacts.of(command.host, user_home=command.session.user_home),
        run=command.host.run,
        timers=command.timers,
        environ=command.config.process_environment(),
        pid=command.pid,
    )
    if step.phase == "services" and not step.force:
        leftover = consume_host_exit_message(command.home)
        if leftover:
            flush_unconsumed_exit_notes(command, env.database_factory, leftover)


def _stop_data_plane_step(
    step: StopDataPlane,
    env: StopEnvironment,
    run: StopRun,
    retained_children: list[subprocess.Popen[bytes]] | None,
) -> StopOutcome | None:
    command = env.command
    config = data_plane_config_from_boot(command.config)
    if step.force:
        failure = data_plane_failure(
            stop_cluster_instance(
                config,
                home=command.home,
                macos=command.host.platform.system == "darwin",
                timers=command.timers,
                environ=command.config.process_environment(),
                retained_children=retained_children,
                producer=env.producer,
            )
        )
        if failure is not None:
            raise DataPlaneStopError(failure)
        return None
    stopped = maintenance_stop.stop(
        config,
        remaining(run.deadline),
        home=command.home,
        uid=command.host.uid,
        retained_children=retained_children,
        producer=env.producer,
        timers=command.timers,
    )
    return StopOutcome(stopped.notes, stopped.clients)


# meiva:note
# Perform one step, reading the time from the invocation's clock; only the data-plane stop has
# something to report.
def _perform(
    step: StopStep,
    env: StopEnvironment,
    run: StopRun,
    retained_children: list[subprocess.Popen[bytes]] | None,
) -> StopOutcome | None:
    command = env.command
    match step:
        case ClearServing():
            start_serving.clear_serving(command.home)
        case Announce(window):
            announce_stopping(command, window, now=command.clock.now())
        case RecordBrowserSession():
            browser.record_browser_session(command, now=command.clock.now())
        case StopServices():
            _stop_services_step(step, env, run)
        case StopBrowser():
            browser.stop_browser(run.deadline, home=command.home)
        case ReapChrome():
            browser.reap_cluster_chrome(home=command.home)
        case CloseShellSessions(direct_db, force, message):
            if force:
                shell_sessions.force_close_shell_sessions(command)
            else:
                shell_sessions.close_shell_sessions(
                    run.deadline,
                    direct_db=direct_db,
                    database_factory=env.database_factory,
                    command=command,
                    cleanup_wait_s=env.shell_cleanup_wait_s,
                    message=message,
                )
        case StopExtras(retained):
            extras.stop_extras(command, run.deadline, retained=retained)
        case StopDataPlane():
            return _stop_data_plane_step(step, env, run, retained_children)
        case SkipDataPlane(plane):
            print(data_plane_skip_line(plane))
        case _:
            assert_never(step)
    return None

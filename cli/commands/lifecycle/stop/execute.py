# meiva:note
# The shell of a stop: perform a plan's steps one at a time, each against the real host.
#
# What to do, in what order and what to keep is `plan.plan_stop`'s answer; this module only
# executes it. A normal stop times every phase (printed before the phase runs, so a caller whose
# outer budget cuts the command off still saw which stage it entered, and recorded in the
# lifecycle journal); a force stop runs its steps untimed.

from __future__ import annotations

import subprocess
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO, assert_never

from base.agents.context.clients import DatabaseFactory
from base.clock import Clock
from base.clock.configuration import clock_config_from_boot
from base.deploy.lifecycle import start_serving
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
    deadline: float
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
    clock = Clock(clock_config_from_boot(env.command.config), timers=env.command.timers)
    for step in plan.steps:
        with timed_phase(
            env.command.home, env.command.pid, run.phases, phase_label(step) if timed else None
        ):
            reported = _perform(step, env, run, retained_children, clock=clock)
        if reported is not None:
            outcome = reported
    return outcome


# meiva:note
# Perform one step, reading the time from the stop's one `clock`; only the data-plane stop has
# something to report.
def _perform(
    step: StopStep,
    env: StopEnvironment,
    run: StopRun,
    retained_children: list[subprocess.Popen[bytes]] | None,
    *,
    clock: Clock,
) -> StopOutcome | None:
    command = env.command
    match step:
        case ClearServing():
            start_serving.clear_serving(command.home)
        case Announce(window):
            announce_stopping(command, window, now=clock.now())
        case RecordBrowserSession():
            browser.record_browser_session(command, now=clock.now())
        case StopServices(preserve, force, _phase):
            root_driver.stop_root_service_tree(
                preserve=preserve,
                timeout_s=remaining(run.deadline),
                force=force,
                helper=command.helper,
                home=command.home,
            )
        case StopBrowser():
            browser.stop_browser(run.deadline)
        case ReapChrome():
            browser.reap_cluster_chrome()
        case CloseShellSessions(direct_db, force):
            if force:
                shell_sessions.force_close_shell_sessions(command.home)
            else:
                shell_sessions.close_shell_sessions(
                    run.deadline,
                    direct_db=direct_db,
                    database_factory=env.database_factory,
                    command=command,
                    cleanup_wait_s=env.shell_cleanup_wait_s,
                )
        case StopExtras(retained):
            extras.stop_extras(command, run.deadline, retained=retained)
        case StopDataPlane(force):
            config = data_plane_config_from_boot(command.config)
            if force:
                failure = data_plane_failure(
                    stop_cluster_instance(
                        config,
                        home=command.home,
                        macos=command.host.platform.system == "darwin",
                        retained_children=retained_children,
                        producer=env.producer,
                    )
                )
                if failure is not None:
                    raise DataPlaneStopError(failure)
            else:
                stopped = maintenance_stop.stop(
                    config,
                    remaining(run.deadline),
                    home=command.home,
                    retained_children=retained_children,
                    producer=env.producer,
                )
                return StopOutcome(stopped.notes, stopped.clients)
        case SkipDataPlane(plane):
            print(data_plane_skip_line(plane))
        case _:
            assert_never(step)
    return None

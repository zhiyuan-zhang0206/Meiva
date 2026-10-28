# meiva:note
# Local stop over the shared native stop boundary.
#
# Normal paths retain checkpoints and never escalate a service or data-plane stop on timeout;
# a normal stop's shell-session closure SIGKILLs what outlives its bounded grace. Explicit force
# uses the separate resource teardown. Every keep-or-stop choice (shell sessions, browser,
# extras, data plane, other services), the interruption mode and the budget are the caller's
# explicit arguments; nothing here defaults or infers one. The caller's registry of the processes
# it spawned (`retained_children`) is an argument of the stop as well: it is a list the caller
# keeps mutating, so it stays out of the frozen `StopEnvironment`.
#
# The stop is a plan and a shell. `plan.plan_stop` turns the choices and the host facts read
# here into the steps (what runs, in what order, what is kept); `execute` performs them. This
# module reads the facts, shows the scope, asks the operator, and reports how the stop ended.

from __future__ import annotations

import subprocess
from dataclasses import replace
from pathlib import Path
from typing import TextIO

from base.cluster import session_name
from base.deploy.lifecycle.home_lifecycle_locks import resource_lock
from base.deploy.lifecycle.status_journal import begin
from base.host.proc import hosting_exec_domain
from base.sessions.hosting import hosting_supervised_session
from base.sessions.pty.paths import SERVICE_UNIT
from cli.commands.data_plane.stop_budget import require_valid_timeout
from cli.commands.data_plane.stop_waits import deadline_after
from cli.commands.lifecycle import root_driver
from cli.commands.lifecycle.stop.admission import CONFIRM_PROMPT, confirmed, hosted_stop_refusal
from cli.commands.lifecycle.stop.choices import (
    BROWSER_SESSION,
    StopChoices,
    StopHost,
    StopRequest,
    StopRoster,
)
from cli.commands.lifecycle.stop.evidence import require_unstarted_initialization
from cli.commands.lifecycle.stop.execute import StopEnvironment, StopRun, execute_plan
from cli.commands.lifecycle.stop.force_outcome import DataPlaneStopError
from cli.commands.lifecycle.stop.plan import plan_stop, require_consistent
from cli.commands.lifecycle.stop.render import preview_lines
from cli.commands.lifecycle.stop.report import (
    finish_stop,
    report_force_incomplete,
    report_incomplete,
)
from cli.commands.lifecycle.stop.steps import StopPlan
from cli.database.lifecycle_audit import operator_lifecycle_audit
from services.supervision.roster.composition import build_services

# meiva:note
# What a stop's failure looks like: reported with its phase accounting, never compensated.
_STOP_FAILURES = (RuntimeError, TimeoutError, OSError, subprocess.TimeoutExpired)


# meiva:note
# A stop run from inside the work it drains strands itself mid-drain: refuse.
def refuse_hosted_stop(home: Path) -> None:
    refusal = hosted_stop_refusal(hosting_exec_domain(), hosting_supervised_session(home))
    if refusal is not None:
        raise RuntimeError(refusal)


# meiva:note
# The roles and the roster this stop is planned against, read from the host once.
#
# A force stop reads no roster: the roster's ports come from the settings, and force is the
# stop that must still work when they cannot be loaded.
def _host(env: StopEnvironment, *, force: bool) -> StopHost:
    if force:
        roster = None
    else:
        specs = build_services(home=env.command.home)
        roster = StopRoster(
            known=frozenset(spec.session for spec in specs),
            requires_db=frozenset(spec.session for spec in specs if spec.requires_db),
        )
    return StopHost(
        roles=env.command.machine.role(), roster=roster, pty_unit=SERVICE_UNIT, unstarted=False
    )


def _agrees(stdin: TextIO | None) -> bool:
    if stdin is None:
        raise ValueError("a stop that asks for confirmation needs a stdin to read the answer from")
    print(CONFIRM_PROMPT, end="", flush=True)
    return confirmed(stdin.readline())


# meiva:note
# Shared stop kernel; only explicit force escalates a service stop.
#
# A refusal (a contradictory choice, a stop run from inside the work it drains) is raised
# before a single unit is signalled.
def do_stop(
    request: StopRequest,
    env: StopEnvironment,
    retained_children: list[subprocess.Popen[bytes]] | None,
) -> int:
    choices: StopChoices = request.choices
    require_consistent(choices, pty_unit=SERVICE_UNIT)
    require_valid_timeout(request.timeout)
    with resource_lock(home=env.command.home, pid=env.command.pid, purpose="cli.do_stop"):
        if not choices.force:
            refuse_hosted_stop(env.command.home)
        host = _host(env, force=choices.force)
        plan = plan_stop(choices, host, request.window)
        if not choices.force:
            # meiva:note
            # Positive first-start evidence turns the stop into the close of an attempt that never
            # admitted work; it is read-only, so it precedes the confirm gate.
            try:
                unstarted = require_unstarted_initialization(env.command.home)
            except _STOP_FAILURES as exc:
                report_incomplete(
                    exc, [], home=env.command.home, owns_journal=False, journal_opened=False
                )
                return 1
            if unstarted:
                plan = plan_stop(choices, replace(host, unstarted=True), request.window)
        if not _admitted(plan, request, env):
            return 0
        if choices.force:
            return _run_force(plan, request, env, retained_children)
        return _run_normal(plan, request, env, retained_children)


# meiva:note
# Show the stop's scope and, when the operator asked for it, wait for a yes.
def _admitted(plan: StopPlan, request: StopRequest, env: StopEnvironment) -> bool:
    tree = plan.scope.tree_preserve
    sessions = (
        ()
        if tree is None
        else tuple(
            root_driver.root_tree_plan(
                tree,
                home=env.command.home,
                environ=env.command.config.process_environment(),
            )
        )
    )
    for line in preview_lines(plan.scope, sessions, session_name(BROWSER_SESSION)):
        print(line)
    if request.require_confirmation and not _agrees(env.stdin):
        print("aborted")
        return False
    return True


def _run_force(
    plan: StopPlan,
    request: StopRequest,
    env: StopEnvironment,
    retained_children: list[subprocess.Popen[bytes]] | None,
) -> int:
    run = StopRun(deadline_after(env.command.timers, request.timeout), [])
    try:
        execute_plan(plan, env, run, retained_children, timed=False)
    except DataPlaneStopError as exc:
        report_force_incomplete(exc)
        return 1
    return 0


def _run_normal(
    plan: StopPlan,
    request: StopRequest,
    env: StopEnvironment,
    retained_children: list[subprocess.Popen[bytes]] | None,
) -> int:
    deadline = deadline_after(env.command.timers, request.timeout)
    home = env.command.home
    owns_journal = begin(home, "stop", pid=env.command.pid, deadline=deadline.at)
    # meiva:note
    # Per-phase accounting: a failed stop names each phase and how much of the shared budget it
    # consumed, so the operator sees WHERE the budget went, never a bare timeout (#2045).
    run = StopRun(deadline, [])
    try:
        outcome = execute_plan(plan, env, run, retained_children, timed=True)
    except _STOP_FAILURES as exc:
        report_incomplete(
            exc, run.phases, home=home, owns_journal=owns_journal, journal_opened=True
        )
        return 1
    return finish_stop(
        home, owns_journal=owns_journal, notes=outcome.notes, clients=outcome.clients
    )


def cmd_stop(
    request: StopRequest,
    env: StopEnvironment,
    retained_children: list[subprocess.Popen[bytes]] | None,
) -> int:
    # meiva:note
    # Stop this unit as the operator chose; retain its data.
    #
    # `keep_infra` is None exactly on a host without the gateway capability (no local data
    # plane); every other choice is a required explicit answer.
    with operator_lifecycle_audit(
        database_factory=env.database_factory,
        environ=env.command.config.process_environment(),
        home=env.command.home,
    ) as audit:
        code = do_stop(request, env, retained_children)
        audit.exited(code)
        return code

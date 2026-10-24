# meiva:note
# `meiva restart`: stop then start the service tree, or restart agent-host alone.
#
# Hosted agents settle through the agent-host's own SIGTERM exit in the shared stop boundary.
# Explicit force authorizes interrupting resource shutdown. `timeout` bounds the stop leg; the
# start leg launches exactly the named service selection. What a restart refuses, which stop it
# runs and what each leg's result means is `restart_plan`'s; this module runs the legs.

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass

from base.deploy.lifecycle import status_journal
from base.host.exit_codes import RESTART_DECLINED_EXIT_CODE
from base.host.proc import hosting_exec_domain
from base.native_process.root_control.ipc import ResponsePayload
from base.paths import prod_service_checkout_error
from base.sessions.hosting import hosting_supervised_session
from base.telemetry import EventPipeline
from cli.commands import _repo
from cli.commands._repo import repo_root
from cli.commands.lifecycle import root_driver, root_readiness, start
from cli.commands.lifecycle.root_control.snapshot import root_units
from cli.commands.lifecycle.start import readiness_preflight
from cli.commands.lifecycle.stop import do_stop, refuse_hosted_stop
from cli.commands.lifecycle.stop.execute import StopEnvironment
from cli.commands.lifecycle.stop.restart_plan import (
    AGENT_HOST,
    NO_ROOT_REFUSAL,
    Finish,
    Leg,
    RestartRequest,
    budget_line,
    missing_unit_refusal,
    restart_refusal,
    restart_start_leg,
    restart_stop_request,
    settle_leg,
)
from cli.database import OperatorDatabaseFactory
from cli.database.lifecycle_audit import operator_lifecycle_audit, record_lifecycle_request
from cli.database.runtime import CommandRuntime
from cli.start_runtime import StartRuntime
from services.supervision.roster import build_services
from services.supervision.roster.service_spec import ServiceSpec


# meiva:note
# Recheck the captured runtime before disruptive work.
def require_restart_runtime(runtime: StartRuntime) -> None:
    runtime.validate()
    if problem := prod_service_checkout_error(runtime.code_root):
        raise ValueError(problem)


# meiva:note
# Admit the currently executing checkout before restart effects.
def restart_runtime() -> StartRuntime:
    runtime = StartRuntime.development(repo_root())
    require_restart_runtime(runtime)
    return runtime


# meiva:note
# What one restart's legs share: its request, its runtime and the caller's handles.
@dataclass(frozen=True)
class _Invocation:
    request: RestartRequest
    runtime: StartRuntime
    command: CommandRuntime
    database_factory: OperatorDatabaseFactory
    producer: Callable[[], EventPipeline]
    retained_children: list[subprocess.Popen[bytes]]


def _run_leg(leg: Leg, run: _Invocation) -> int:
    request, runtime, command = run.request, run.runtime, run.command
    match leg:
        case Leg.PROBES:
            # meiva:note
            # Probe the gateway and register the machine BEFORE stopping services: a transient
            # gateway outage or network blip would otherwise leave the host in "services dead,
            # can't start" after the stop below. On failure the host keeps serving.
            return _repo.preflight_probes(run.database_factory(), command=command)
        case Leg.READINESS:
            # meiva:note
            # The read-only local checks of the start leg below. `check_launcher=False`: this
            # start is in-process and never execs `.venv/bin/meiva`; the gate's interpreter
            # check covers the venv seam the session launches actually use.
            return readiness_preflight.preflight_start_readiness(
                runtime.code_root,
                home=command.home,
                check_launcher=False,
                runtime=runtime,
                only_services=request.only_services,
                disabled_services=request.disabled_services,
                all_services=request.all_services,
                machine=command.machine,
                host=command.host,
            )
        case Leg.STOP:
            # meiva:note
            # Every restart uses the shared hosted stop kernel; a timeout never silently
            # authorizes force.
            require_restart_runtime(runtime)
            return do_stop(
                restart_stop_request(request, roles=command.machine.role()),
                StopEnvironment(
                    command=command,
                    database_factory=run.database_factory,
                    producer=run.producer,
                    retained_children=run.retained_children,
                    stdin=None,
                ),
            )
        case Leg.START:
            # meiva:note
            # Keep the captured runtime and the operator's selection through startup; neither
            # is recaptured from a later caller or moving selector.
            launch = restart_start_leg(request)
            return start.cmd_start_body(
                run.database_factory,
                only_services=launch.only_services,
                disabled_services=launch.disabled_services,
                all_services=launch.all_services,
                reconcile=launch.reconcile,
                ready_timeout=launch.ready_timeout,
                runtime=runtime,
                retained_children=run.retained_children,
                producer=run.producer,
                command=command,
            )


# meiva:note
# Run the legs in order until one ends the restart.
def _restart(run: _Invocation) -> int:
    print(f"[meiva restart] cwd = {run.runtime.code_root}")
    # meiva:note
    # When an outer operation's journal is still running, this restart records phases into it
    # and leaves the finish to that caller (`conclude`).
    owns_journal = status_journal.begin("restart")
    print(
        budget_line(
            run.request.timeout, run.request.start_timeout, str(status_journal.status_path())
        ),
        flush=True,
    )
    for leg in Leg:
        if leg.heading:
            print(leg.heading)
        with status_journal.phase(leg.phase):
            rc = _run_leg(leg, run)
        outcome = settle_leg(leg, rc)
        if isinstance(outcome, Finish):
            return conclude(outcome, owns_journal=owns_journal)
    raise AssertionError("the start leg always ends a restart")


# meiva:note
# End the restart: say why on stderr, close the journal when this restart opened it.
#
# Only the journal's opener closes it. An outer operation's still-running journal (the updater
# wrapping this restart) is left to that caller, so its diagnosis is not clobbered (task #2898).
def conclude(outcome: Finish, *, owns_journal: bool) -> int:
    if outcome.notice is not None:
        print(outcome.notice, file=sys.stderr)
    if owns_journal:
        status_journal.finish(outcome.rc, error=outcome.error)
    return outcome.rc


def _cmd_restart_body(
    request: RestartRequest,
    *,
    database_factory: OperatorDatabaseFactory,
    producer: Callable[[], EventPipeline],
    command: CommandRuntime,
    retained_children: list[subprocess.Popen[bytes]] | None,
) -> int:
    if retained_children is None:
        raise ValueError("PostgreSQL launch requires its caller-owned child retention")
    # meiva:note
    # Admission precedes even the restart journal: an incompatible caller must leave the running
    # generation untouched.
    runtime = restart_runtime()
    refusal = restart_refusal(hosting_exec_domain(), hosting_supervised_session())
    if refusal is not None:
        print(refusal.message, file=sys.stderr)
        return RESTART_DECLINED_EXIT_CODE
    return _restart(
        _Invocation(request, runtime, command, database_factory, producer, retained_children)
    )


def cmd_restart(
    *,
    only_services: tuple[str, ...],
    disabled_services: tuple[str, ...],
    all_services: bool,
    keep_shell_sessions: bool,
    keep_browser: bool,
    restart_extras: bool,
    force: bool,
    timeout: float,
    start_timeout: float,
    retained_children: list[subprocess.Popen[bytes]] | None,
    database_factory: OperatorDatabaseFactory,
    producer: Callable[[], EventPipeline],
    command: CommandRuntime,
) -> int:
    # meiva:note
    # Stop then start the service tree without a confirmation prompt.
    request = RestartRequest(
        only_services=only_services,
        disabled_services=disabled_services,
        all_services=all_services,
        keep_shell_sessions=keep_shell_sessions,
        keep_browser=keep_browser,
        restart_extras=restart_extras,
        force=force,
        timeout=timeout,
        start_timeout=start_timeout,
    )
    with operator_lifecycle_audit(database_factory=database_factory) as audit:
        code = _cmd_restart_body(
            request,
            database_factory=database_factory,
            producer=producer,
            command=command,
            retained_children=retained_children,
        )
        audit.exited(code)
        return code


def _require_ok(response: ResponsePayload, what: str) -> None:
    if not response["ok"]:
        raise root_driver.RootDriverError(
            f"{what} failed: {response.get('code')}: {response.get('error')}"
        )


# meiva:note
# `meiva restart --only agent-host`: stop agent-host and let its root bring it back.
#
# The scope is agent-host alone, so shell sessions, the browser, the extras, every other service
# and the data plane are outside this operation: there is no keep-or-stop choice to make. The
# operator names the interruption mode and the stop budget. Agent-host settles its own in-flight
# turns on SIGTERM; a unit that outlives the root's stop window is the root's to kill.
#
# Down agent-host through its live root (`force_down` only with `force`), then up.
#
# `spec` is the agent-host service whose protocol readiness is waited for, up to
# `start_timeout`. A root this unit does not own is refused.
def restart_agent_host(
    command: CommandRuntime,
    spec: ServiceSpec,
    *,
    force: bool,
    timeout: float,
    start_timeout: float,
) -> int:
    client = root_driver.root_client(command.home, timeout=timeout)
    status = root_driver.root_status(client)
    if status is None:
        print(NO_ROOT_REFUSAL, file=sys.stderr)
        return 1
    root_driver.require_root_owner(status, helper=command.helper, home=command.home)
    if (refusal := missing_unit_refusal(tuple(root_units(status)))) is not None:
        print(refusal, file=sys.stderr)
        return 1
    record_lifecycle_request("force-down" if force else "down", [AGENT_HOST])
    _require_ok(
        client.force_down(AGENT_HOST) if force else client.down(AGENT_HOST),
        f"down {AGENT_HOST}",
    )
    print(f"  ✓ {AGENT_HOST} stopped")
    record_lifecycle_request("up", [AGENT_HOST])
    fresh = root_driver.root_client(command.home, timeout=timeout)
    _require_ok(fresh.up(AGENT_HOST), f"up {AGENT_HOST}")
    waited = root_readiness.wait_for_service_tree(
        (spec,), timeout_s=start_timeout, home=command.home
    )
    if waited.unready:
        print(f"  ✗ {AGENT_HOST} did not become ready after restart", file=sys.stderr)
        return 1
    print(f"  ✓ {AGENT_HOST} restarted and ready")
    return 0


def cmd_restart_agent_host(
    *,
    force: bool,
    timeout: float,
    start_timeout: float,
    database_factory: OperatorDatabaseFactory,
    command: CommandRuntime,
) -> int:
    # meiva:note
    # Stop agent-host within `timeout` (force-killing only with `force`), then bring it up and
    # wait up to `start_timeout` for its readiness.
    with operator_lifecycle_audit(database_factory=database_factory) as audit:
        try:
            refuse_hosted_stop()
            spec = next(spec for spec in build_services() if spec.session == AGENT_HOST)
            code = restart_agent_host(
                command, spec, force=force, timeout=timeout, start_timeout=start_timeout
            )
        except (RuntimeError, OSError) as exc:
            print(f"  ✗ restart of {AGENT_HOST} incomplete: {exc}", file=sys.stderr)
            code = 1
        audit.exited(code)
        return code

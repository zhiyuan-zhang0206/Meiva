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
from pathlib import Path
from typing import assert_never

from base.deploy.lifecycle import status_journal
from base.host.exit_codes import RESTART_DECLINED_EXIT_CODE
from base.host.proc import hosting_exec_domain
from base.native_process.root_control.client import RootClient
from base.native_process.root_control.ipc import ResponsePayload
from base.paths.home import home_checkout_error
from base.sessions.hosting import hosting_supervised_session
from base.telemetry import EventPipeline
from cli.commands import _repo
from cli.commands.lifecycle import root_driver, root_readiness, start
from cli.commands.lifecycle.root_control.snapshot import root_units
from cli.commands.lifecycle.start import readiness_preflight
from cli.commands.lifecycle.stop import do_stop, refuse_hosted_stop
from cli.commands.lifecycle.stop.execute import StopEnvironment
from cli.commands.lifecycle.stop.restart_plan import (
    AGENT_HOST,
    NO_ROOT_REFUSAL,
    Continue,
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
# Admit the currently executing checkout for a restart of `home`, before any restart effect.
#
# A checkout the home's own `source` forbids is refused before its source is hashed; the
# returned runtime is held through both legs, so a later caller or moving selector cannot
# change what the start leg executes.
def restart_runtime(checkout: Path, *, home: Path) -> StartRuntime:
    if problem := home_checkout_error(checkout, home=home):
        raise ValueError(problem)
    return StartRuntime.admit(checkout)


# meiva:note
# What one restart's legs share: its request, its runtime and the caller's handles. The caller's
# registry of spawned processes is a mutable list, so it is passed beside this value, not in it.
@dataclass(frozen=True)
class _Invocation:
    request: RestartRequest
    runtime: StartRuntime
    command: CommandRuntime
    database_factory: OperatorDatabaseFactory
    producer: Callable[[], EventPipeline]


def _run_leg(leg: Leg, run: _Invocation, retained_children: list[subprocess.Popen[bytes]]) -> int:
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
            return do_stop(
                restart_stop_request(request, roles=command.machine.role()),
                StopEnvironment(
                    command=command,
                    database_factory=run.database_factory,
                    producer=run.producer,
                    stdin=None,
                ),
                retained_children,
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
                retained_children=retained_children,
                producer=run.producer,
                command=command,
            )
        case _:
            assert_never(leg)


# meiva:note
# Run the legs in order until one ends the restart.
def _restart(run: _Invocation, retained_children: list[subprocess.Popen[bytes]]) -> int:
    print(f"[meiva restart] cwd = {run.runtime.code_root}")
    # meiva:note
    # When an outer operation's journal is still running, this restart records phases into it
    # and leaves the finish to that caller (`conclude`).
    home = run.command.home
    owns_journal = status_journal.begin(home, "restart")
    print(
        budget_line(
            run.request.timeout, run.request.start_timeout, str(status_journal.status_path(home))
        ),
        flush=True,
    )
    for leg in Leg:
        if leg.heading:
            print(leg.heading)
        with status_journal.phase(home, leg.phase):
            rc = _run_leg(leg, run, retained_children)
        outcome = settle_leg(leg, rc)
        match outcome:
            case Finish():
                return conclude(home, outcome, owns_journal=owns_journal)
            case Continue():
                pass
            case _:
                assert_never(outcome)
    raise AssertionError("the start leg always ends a restart")


# meiva:note
# End the restart: say why on stderr, close the journal when this restart opened it.
#
# Only the journal's opener closes it. An outer operation's still-running journal (the updater
# wrapping this restart) is left to that caller, so its diagnosis is not clobbered (task #2898).
def conclude(home: Path, outcome: Finish, *, owns_journal: bool) -> int:
    if outcome.notice is not None:
        print(outcome.notice, file=sys.stderr)
    if owns_journal:
        status_journal.finish(home, outcome.rc, error=outcome.error)
    return outcome.rc


def _cmd_restart_body(
    request: RestartRequest,
    *,
    runtime: StartRuntime,
    database_factory: OperatorDatabaseFactory,
    producer: Callable[[], EventPipeline],
    command: CommandRuntime,
    retained_children: list[subprocess.Popen[bytes]],
) -> int:
    refusal = restart_refusal(hosting_exec_domain(), hosting_supervised_session(command.home))
    if refusal is not None:
        print(refusal.message, file=sys.stderr)
        return RESTART_DECLINED_EXIT_CODE
    return _restart(
        _Invocation(request, runtime, command, database_factory, producer), retained_children
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
    runtime: StartRuntime,
    retained_children: list[subprocess.Popen[bytes]],
    database_factory: OperatorDatabaseFactory,
    producer: Callable[[], EventPipeline],
    command: CommandRuntime,
) -> int:
    # meiva:note
    # Stop then start the service tree without a confirmation prompt.
    #
    # `runtime` was admitted before this call (`restart_runtime`), so an incompatible caller
    # never reaches the journal and leaves the running generation untouched.
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
            runtime=runtime,
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
# Admit `meiva restart --only agent-host`: the live root it goes through, or None after the
# refusal is said.
#
# Nothing is built and nothing is asked of the root before this answers, so a host whose settings
# cannot be loaded still reports the plain refusal: no live root on this unit, a root this unit
# does not own (`require_root_owner` raises), or a root that runs no agent-host unit.
def admit_agent_host_restart(command: CommandRuntime, *, timeout: float) -> RootClient | None:
    client = root_driver.root_client(command.home, timeout=timeout)
    status = root_driver.root_status(client)
    if status is None:
        print(NO_ROOT_REFUSAL, file=sys.stderr)
        return None
    root_driver.require_root_owner(status, helper=command.helper, home=command.home)
    if (refusal := missing_unit_refusal(tuple(root_units(status)))) is not None:
        print(refusal, file=sys.stderr)
        return None
    return client


# meiva:note
# `meiva restart --only agent-host`: stop agent-host and let its root bring it back.
#
# The scope is agent-host alone, so shell sessions, the browser, the extras, every other service
# and the data plane are outside this operation: there is no keep-or-stop choice to make. The
# operator names the interruption mode and the stop budget. Agent-host settles its own in-flight
# turns on SIGTERM; a unit that outlives the root's stop window is the root's to kill.
#
# Down agent-host through the admitted root `client` (`force_down` only with `force`), then up.
#
# `spec` is the agent-host service whose protocol readiness is waited for, up to
# `start_timeout`.
def restart_agent_host(
    command: CommandRuntime,
    client: RootClient,
    spec: ServiceSpec,
    *,
    force: bool,
    timeout: float,
    start_timeout: float,
) -> int:
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
            refuse_hosted_stop(command.home)
            client = admit_agent_host_restart(command, timeout=timeout)
            if client is None:
                code = 1
            else:
                spec = next(spec for spec in build_services() if spec.session == AGENT_HOST)
                code = restart_agent_host(
                    command, client, spec, force=force, timeout=timeout, start_timeout=start_timeout
                )
        except (RuntimeError, OSError) as exc:
            print(f"  ✗ restart of {AGENT_HOST} incomplete: {exc}", file=sys.stderr)
            code = 1
        audit.exited(code)
        return code

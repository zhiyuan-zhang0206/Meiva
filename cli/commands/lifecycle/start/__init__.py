# meiva:note
# Single start lifecycle: persisted identity, prepared storage, one ready root tree.
#
# Exit zero means the selected core services are ready. Optional capabilities can remain
# unavailable without preventing the host from serving.
#
# The start is a shell around plans. Each decision (the stand-down record, a refused home or
# capability set, the cold preparation and its order, the schema checks, the gateway probe, the
# readiness verdict and the texts a refusal prints) is a `strict` module of this package that
# takes values and returns a plan or a line; this module reads the facts, runs each step in
# order and stops at the first that fails.

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from base import cluster
from base.cluster import session_name
from base.cluster.machine import MachineIdentity, MachineRoles
from base.deploy import start_trace
from base.deploy.lifecycle import launch_failures, pause_markers, start_serving
from base.deploy.lifecycle.service_selection import resolve_selection
from base.deploy.progress_timeout import CRITICAL_SERVICE_SESSIONS, SERVICE_READY_TIMEOUT_S
from base.host.system.hostport import Host
from base.native_process.os_platform import raise_fd_limit
from base.paths.home import home_checkout_error
from base.telemetry import EventPipeline
from cli.commands import _repo, _setup, probe
from cli.commands._setup import SetupValues
from cli.commands.data_plane.bringup import read_launch_delivery
from cli.commands.lifecycle import root_driver, root_readiness, stand_down
from cli.commands.lifecycle._exclusive_resources import exclusive_resources
from cli.commands.lifecycle.root_control.generation import record_running_sha
from cli.commands.lifecycle.root_control.launch_plan import RootLaunchInputs
from cli.commands.lifecycle.start.admission import (
    capability_refusal,
    guard_refusal,
    probes_gateway,
    roles_line,
)
from cli.commands.lifecycle.start.cold_start import ColdStart, cold_start_steps, prepare_cold_start
from cli.commands.lifecycle.start.cold_start_plan import SchemaCheck, plan_schema_checks
from cli.commands.lifecycle.start.gateway_hint import gateway_hint_lines
from cli.commands.lifecycle.start.login_link import print_login_link
from cli.commands.lifecycle.start.occupied_ports import Occupant, refusal_text
from cli.commands.lifecycle.start.readiness_plan import (
    ReadinessFacts,
    archive_assertion_needed,
    critical_launch_failures,
    shows_gateway_hint,
)
from cli.commands.lifecycle.start.readiness_plan import readiness_verdict as plan_readiness
from cli.commands.lifecycle.start.stand_down_gate import stand_down_step
from cli.commands.lifecycle.start.start_config import StartConfig, read_start_config
from cli.commands.lifecycle.status import cmd_status
from cli.database import OperatorDatabaseFactory
from cli.database.lifecycle_audit import operator_lifecycle_audit
from cli.database.runtime import CommandRuntime
from cli.start_identity import mark_phase, read_intent
from cli.start_runtime import StartRuntime
from services.supervision.roster.service_spec import ServiceSpec
from services.supervision.roster.spec import services_for_capabilities_annotated


# meiva:note
# Refuse known core-port conflicts; optional capabilities cannot block start.
def refuse_occupied_health_ports(roster: tuple[ServiceSpec, ...]) -> int:
    occupied = probe.occupied_health_ports(
        tuple(s for s in roster if s.session in CRITICAL_SERVICE_SESSIONS)
    )
    if not occupied:
        return 0
    print(
        refusal_text(
            tuple(
                Occupant(port.spec.session, session_name(port.spec.session), port.detail)
                for port in occupied
            )
        ),
        file=sys.stderr,
    )
    return 1


@dataclass(frozen=True)
class _Selection:
    # meiva:note
    # The caller's explicit service selection for this start.
    #
    # `reconcile` refuses a live root that runs units outside the selection; restart
    # leaves it off because the units its stop kept still run.

    only_services: tuple[str, ...]
    disabled_services: tuple[str, ...]
    all_services: bool
    reconcile: bool


# meiva:note
# What every phase of one start reads: the invocation's values, resolved once.
@dataclass(frozen=True)
class _Start:
    command: CommandRuntime
    database_factory: OperatorDatabaseFactory
    producer: Callable[[], EventPipeline]
    runtime: StartRuntime
    selection: _Selection
    resolved: SetupValues
    roles: MachineRoles
    config: StartConfig
    launch_inputs: RootLaunchInputs

    @property
    def repo(self) -> Path:
        return self.runtime.code_root


def _stand_down(home: Path, *, boot_attempt: bool) -> int | None:
    # meiva:note
    # A stop that says "stopped" outranks this start when the OS boot job is the
    # caller: the boot job exists for an accidental death, not to undo an
    # operator's stop (task #5493). An explicit start is the one thing that ends
    # a stand-down, so it clears the record before anything is admitted or launched.
    held = stand_down.read(home)
    now = datetime.now(UTC)
    step = stand_down_step(
        boot_attempt=boot_attempt,
        recorded=held is not None,
        deadline_passed=held is not None and held.deadline_passed(now),
        window="" if held is None else held.describe(now),
    )
    if step.clear:
        stand_down.clear(home)
    if step.line is not None:
        print(f"  · {step.line}")
    return 0 if step.skip else None


# meiva:note
# Refuse a destroyed home or a disposable prod checkout.
def _guard(command: CommandRuntime, runtime: StartRuntime) -> int | None:
    refusal = guard_refusal(
        destroy_intent=(command.home / "destroy-intent.json").exists(),
        checkout_error=home_checkout_error(runtime.code_root, home=command.home),
    )
    print(f"[meiva start] cwd = {runtime.code_root}")
    if refusal is None:
        return None
    print(refusal.message, file=sys.stderr)
    return 1


# meiva:note
# Collect the home's recorded setup fields and settle its roles against `meiva init`.
def _resolve_identity(command: CommandRuntime) -> tuple[SetupValues, MachineRoles] | int:
    # meiva:note
    # 0b) collect & validate the home's recorded setup fields (capability-aware filter)
    try:
        resolved, missing = _setup.collect_setup_values(config=command.config)
    except ValueError as e:
        # meiva:note
        # validator failure (e.g. MachineRoleInvalid) — do not persist invalid
        # value, print error + exit.
        print(f"\n✗ {e}", file=sys.stderr)
        return 1
    if missing:
        print(_setup.missing_setup_message(missing), file=sys.stderr)
        return 1

    # meiva:note
    # Successful setup refreshes only this invocation's cached identity.
    command.machine.reset()
    roles = command.machine.role()
    recorded = read_intent(command.home)
    refusal = capability_refusal(roles, None if recorded is None else recorded["roles"])
    if refusal is not None:
        print(refusal.message, file=sys.stderr)
        return 1
    print(roles_line(roles, resolved["machine_name"]))
    # meiva:note
    # every service spawned here inherits the raised ceiling
    raise_fd_limit(65536)
    return resolved, roles


def launch_roster(
    roles: MachineRoles,
    *,
    only_services: tuple[str, ...],
    disabled_services: tuple[str, ...],
    all_services: bool,
    machine: MachineIdentity,
    host: Host,
) -> tuple[ServiceSpec, ...]:
    # meiva:note
    # The launch roster of the caller's explicit service selection: exactly one selection form
    # (`resolve_selection` refuses anything else), minus the units this host gates out.
    names = {
        spec.session
        for spec, _reason in services_for_capabilities_annotated(roles, machine=machine, host=host)
    }
    launch_skip = resolve_selection(
        names, only=only_services, excluded=disabled_services, all_services=all_services
    )
    return root_driver.start_roster(roles, launch_skip, machine=machine, host=host)


def _roster_for(start: _Start) -> tuple[ServiceSpec, ...]:
    return launch_roster(
        start.roles,
        only_services=start.selection.only_services,
        disabled_services=start.selection.disabled_services,
        all_services=start.selection.all_services,
        machine=start.command.machine,
        host=start.command.host,
    )


def _check_schema(start: _Start, *, live: bool) -> int | None:
    # meiva:note
    # A live generation may only be inspected here; pending schema changes
    # require a stopped writer boundary and never run as a repeat-start effect.
    db_url = start.database_factory.db_url()
    with start_trace.phase("schema check (post-admission)"):
        for check in plan_schema_checks(live=live):
            match check:
                case SchemaCheck.MEIVA:
                    rc = _repo.assert_schema_current_or_die(db_url)
                    if rc:
                        return rc
                case SchemaCheck.CHECKPOINT:
                    cluster.assert_checkpoint_schema_current(db_url)
    return None


# meiva:note
# Admit the start (or prepare a cold one) and check the schema before anything launches.
def _admit(start: _Start, *, retained_children: list[subprocess.Popen[bytes]]) -> int | None:
    command = start.command
    with start_trace.phase("roster resolve"):
        roster = _roster_for(start)
    try:
        with start_trace.phase("live generation check"):
            live = root_driver.admit_live_start(
                roster,
                start.repo,
                start.roles,
                reconcile=start.selection.reconcile,
                helper=command.helper,
                inputs=start.launch_inputs,
            )
        if not live:
            rc = prepare_cold_start(
                ColdStart(
                    command=command,
                    database_factory=start.database_factory,
                    producer=start.producer,
                    repo=start.repo,
                    roles=start.roles,
                    plane=start.config.data_plane,
                    converge_steps=cold_start_steps(),
                ),
                roster,
                retained_children=retained_children,
            )
            if rc:
                return rc
    except (RuntimeError, OSError, ValueError) as exc:
        print(f"  \u2717 start admission/preparation failed: {exc}", file=sys.stderr)
        return 1
    return _check_schema(start, live=live)


# meiva:note
# Register this host centrally, and (pure runner) probe the gateway before bring-up.
def _register_and_probe(start: _Start) -> int | None:
    # meiva:note
    # UPSERT this host into the machines table. The table is informational for ops
    # (`meiva cluster status`) and drives agent-runner self-update orchestration;
    # gateway->agent-runner RPC dials the host's ops URL stored in this row, so a
    # missing/NULL row means the cluster cannot reach it. Failing here means this host is
    # invisible to the cluster: fatal on both roles.
    print("\n\u2192 register machine in central DB")
    with start_trace.phase("register machine"):
        rc = _repo.register_machine_or_die(
            start.database_factory(), start.resolved, start.roles, command=start.command
        )
    if rc != 0:
        return rc
    if probes_gateway(start.roles):
        # meiva:note
        # The host reaches the gateway over the private network for self-heal updates and
        # cluster status; catching a broken path now puts the failure on this stdout.
        print("\n\u2192 probe gateway")
        with start_trace.phase("gateway probe"):
            rc = _repo.probe_gateway_or_die(start.resolved["gateway_url"])
        if rc != 0:
            return rc
    return None


def _critical_names() -> frozenset[str]:
    return frozenset(session_name(name) for name in CRITICAL_SERVICE_SESSIONS)


def _report_running_tree(
    start: _Start, *, started: tuple[ServiceSpec, ...], wait: probe.ReadinessWait
) -> None:
    # meiva:note
    # The snapshot and the reach-me hint stay one unit so the status block keeps one
    # owner; both read the same launch outcome the verdict below judges.
    print("\n\u2192 status")
    with start_trace.phase("status"):
        cmd_status(database_factory=start.database_factory, command=start.command)
    sessions = {spec.session for spec in started}
    if shows_gateway_hint(sessions, unready=bool(wait.unready)):
        for line in gateway_hint_lines(
            host=start.command.machine.host(),
            port=start.config.gateway_port,
            home=start.command.home,
        ):
            print(line)
        print_login_link(sessions, config=start.config, at_terminal=sys.stdout.isatty())
    _report_browser_session(start)


# meiva:note
# The browser's restore proof, in two counts: URL restoration and the pin/group
# reapply fail independently, so one number would hide the second.
# meiva:link services/desktop/browser/docs/browser-session-restore.meiva.okf.md
def _report_browser_session(start: _Start) -> None:
    from services.desktop.browser import session_restore

    with start_trace.phase("browser session"):
        restored = session_restore.report(
            home=start.command.home,
            chrome=session_restore.CdpChrome(start.config.browser_cdp_port),
        )
        if restored is not None:
            print(f"  {restored}")


# meiva:note
# Report all failures, but gate serving only on selected core services.
def readiness_verdict(
    launch: root_driver.LaunchOutcome, wait: probe.ReadinessWait, *, ready_timeout: float
) -> int | None:
    verdict = plan_readiness(
        ReadinessFacts(
            launch_failed=launch.failed,
            critical=_critical_names(),
            unready=bool(wait.unready),
            non_critical_unready=bool(wait.non_critical_unready),
        )
    )
    if verdict.launch_failure_line is not None:
        print(verdict.launch_failure_line, file=sys.stderr)
    if verdict.report_unready:
        probe.print_unready_services(wait, ready_timeout)
    if verdict.report_non_critical_unready:
        probe.print_non_critical_unready_services(wait.non_critical_unready)
        probe.report_non_critical_unready_services(wait.non_critical_unready)
    return verdict.exit_code


# meiva:note
# The gateway is serving, so its Postgres is up: assert once that it carries the
# archive settings this code configures (services/backup/walg/startup.py).
def _assert_archive_after_readiness(start: _Start, wait: probe.ReadinessWait) -> None:
    gateway_unready = any(spec.session == "gateway" for spec in wait.unready)
    if not archive_assertion_needed(start.roles, gateway_unready=gateway_unready):
        return
    from services.backup.walg.startup import assert_configured_archive

    walg_config_file = start.config.walg_config_file
    assert_configured_archive(
        machine=start.command.machine.name(), path_reader=lambda: walg_config_file
    )


# meiva:note
# A pause marker left by a previous release claims this home is stopped; readiness just made that
# false, so the sweep removes it here and names it in the start's own output.
def _report_pause_markers(home: Path) -> None:
    with start_trace.phase("pause markers"):
        for line in pause_markers.sweep(home):
            print(f"  {line}")


def _launch(
    start: _Start,
    roster: tuple[ServiceSpec, ...],
    *,
    retained_children: list[subprocess.Popen[bytes]],
    boot_attempt: bool,
) -> root_driver.LaunchOutcome:
    with start_trace.phase("launch service tree"):
        launch = root_driver.launch_service_tree(
            roster,
            start.repo,
            start.roles,
            reconcile=start.selection.reconcile,
            retained_children=retained_children,
            helper=start.command.helper,
            runtime=start.runtime,
            honor_restart_budget=boot_attempt,
            inputs=start.launch_inputs,
        )
    # meiva:note
    # Hand the launch failures to whoever runs this start from another process. Written
    # unconditionally so a clean start clears a previous run's list; the rollout's local leg
    # is the consumer (`update._run_gateway_local_update`), because its `meiva start` is a
    # child and an exit code cannot carry names.
    launch_failures.record(
        start.command.home, list(critical_launch_failures(launch.failed, _critical_names()))
    )
    return launch


def _wait_ready(
    start: _Start, launch: root_driver.LaunchOutcome, *, ready_timeout: float
) -> probe.ReadinessWait:
    # meiva:note
    # Success requires real readiness for every launched service, frontend included.
    print("\n\u2192 waiting for services to come up")
    with start_trace.phase("readiness wait"):
        return root_readiness.wait_for_service_tree(
            tuple(launch.started), timeout_s=ready_timeout, home=start.command.home
        )


def _finalize(start: _Start, serving_generation: str) -> int:
    with start_trace.phase("finalize"):
        if not start_serving.mark_serving(serving_generation, runtime=start.runtime.loaded):
            print("  \u2717 this start lost its serving generation", file=sys.stderr)
            return 1
        mark_phase(start.command.home, "ready")
    return 0


@exclusive_resources
def cmd_start_body(
    database_factory: OperatorDatabaseFactory,
    *,
    disabled_services: tuple[str, ...],
    only_services: tuple[str, ...],
    all_services: bool,
    reconcile: bool,
    ready_timeout: float,
    runtime: StartRuntime,
    retained_children: list[subprocess.Popen[bytes]],
    producer: Callable[[], EventPipeline],
    command: CommandRuntime,
    boot_attempt: bool = False,
) -> int:
    # meiva:note
    # Core start logic, shared by cmd_start and cmd_restart.
    #
    # The caller names exactly one service selection; nothing is persisted, so no
    # later start inherits it. ``reconcile`` is off only for restart's start leg.
    with start_trace.phase("stand-down"):
        ended = _stand_down(command.home, boot_attempt=boot_attempt)
    if ended is not None:
        return ended
    with start_trace.phase("admission"):
        refused = _guard(command, runtime)
    if refused is not None:
        return refused
    with start_trace.phase("identity"):
        identity = _resolve_identity(command)
    if isinstance(identity, int):
        return identity
    resolved, roles = identity
    config = read_start_config(command.config)
    start = _Start(
        command=command,
        database_factory=database_factory,
        producer=producer,
        runtime=runtime,
        selection=_Selection(only_services, disabled_services, all_services, reconcile),
        resolved=resolved,
        roles=roles,
        config=config,
        launch_inputs=RootLaunchInputs(
            delivery=read_launch_delivery(
                home=command.home,
                remote_data_plane=config.data_plane.is_remote,
                config=command.config,
            ),
            service_path=config.service_path,
        ),
    )
    admitted = _admit(start, retained_children=retained_children)
    if admitted is not None:
        return admitted
    registered = _register_and_probe(start)
    if registered is not None:
        return registered

    # meiva:note
    # Preparation may materialize plugins on a cold start; resolve the launch roster now.
    with start_trace.phase("roster resolve (launch)"):
        roster = _roster_for(start)

    # meiva:note
    # Probe before binding: refuse to launch a daemon onto a health port another unit already
    # answers on. This is the last point at which nothing has been spawned, and the roster is
    # knowable only after the skip resolves.
    with start_trace.phase("health-port preflight"):
        rc = refuse_occupied_health_ports(roster)
    if rc != 0:
        return rc

    # meiva:note
    # Failed start attempts must leave recovery actions gated.
    with start_trace.phase("serving generation"):
        serving_generation = start_serving.begin_start(command.home)
        record_running_sha(runtime.code_root)
    launch = _launch(start, roster, retained_children=retained_children, boot_attempt=boot_attempt)
    wait = _wait_ready(start, launch, ready_timeout=ready_timeout)
    _report_running_tree(start, started=tuple(launch.started), wait=wait)

    # meiva:note
    # Readiness verdict last: its exit code and printed snapshot describe the same run.
    verdict = readiness_verdict(launch, wait, ready_timeout=ready_timeout)
    if verdict is not None:
        return verdict
    _assert_archive_after_readiness(start, wait)
    _report_pause_markers(command.home)
    return _finalize(start, serving_generation)


def cmd_start(
    *,
    disabled_services: tuple[str, ...],
    only_services: tuple[str, ...],
    all_services: bool,
    runtime: StartRuntime,
    retained_children: list[subprocess.Popen[bytes]],
    database_factory: OperatorDatabaseFactory,
    producer: Callable[[], EventPipeline],
    command: CommandRuntime,
    boot_attempt: bool = False,
) -> int:
    # meiva:note
    # Converge one configured unit through storage, schema, root and readiness.
    # `meiva init` persisted the unit's identity before the public CLI reaches this
    # entry; every caller names its service selection explicitly. `boot_attempt` marks the OS
    # boot job's retried start, which leaves a unit whose meiva-root circuit breaker is open
    # down: the health monitor keeps retrying that unit on its backoff ladder (task #5315).
    with operator_lifecycle_audit(
        database_factory=database_factory, enabled=not boot_attempt
    ) as audit:
        code = cmd_start_body(
            database_factory,
            disabled_services=disabled_services,
            only_services=only_services,
            all_services=all_services,
            reconcile=True,
            ready_timeout=SERVICE_READY_TIMEOUT_S,
            runtime=runtime,
            retained_children=retained_children,
            producer=producer,
            command=command,
            boot_attempt=boot_attempt,
        )
        audit.exited(code)
        return code

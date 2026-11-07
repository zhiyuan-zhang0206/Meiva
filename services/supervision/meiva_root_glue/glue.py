"""Bind actual root units to readiness, diagnostics, and tree self-check.

Service recovery belongs to HealthMonitor; host diagnostics have no launch
verbs. One round coordinator publishes freshness after both parts complete.
Native data-plane custody and the macOS parent helper remain explicit external
boundaries; diagnostics never repair them from inside the service subtree.
"""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile
from collections.abc import Callable, Mapping, MutableMapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from base.agents.context.clients import ClientSet
from base.cluster.dataplane.direct_url import ClusterDirectUrl
from base.cluster.machine.configured import ConfiguredMachineIdentity
from base.config import ConfigBoot, get_field, settings
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.db.config import db_config_from_boot
from base.deploy.progress_timeout import (
    CRITICAL_SERVICE_SESSIONS,
    NON_CRITICAL_SERVICE_READY_TIMEOUT_S,
    SERVICE_READY_TIMEOUT_S,
)
from base.host.exit_codes import hard_exit
from base.host.proc import CommandRunner
from base.host.system.hostport import Host
from base.native_process.child_env import inherited_process_env
from base.native_process.code_version import CodeVersion
from base.native_process.loaded_commit import LoadedCommit
from base.native_process.os_platform import Platform
from base.telemetry import build_pipeline
from base.telemetry.lgtm_local import LgtmSettings, backend_urls
from services.desktop.permissions_helper.launchd_job import LaunchdFacts
from services.supervision.meiva_root.health import HealthConfig, HealthMonitor, LeaseProbe
from services.supervision.meiva_root.probes import Probe, ProbeError, ProbeRegistry, ProbeSource
from services.supervision.meiva_root.selfcheck import SelfCheckConfig, TreeSelfCheck
from services.supervision.meiva_root.supervisor import Supervisor
from services.supervision.meiva_root.wiring import (
    ResourceHandler,
    WiringContext,
    WiringParticipant,
)
from services.supervision.meiva_root_glue.diagnostic_probes import (
    ProbeConfig,
    build_diagnostics,
    database_recovery_probe,
    live_lease_probe,
)
from services.supervision.meiva_root_glue.diagnostics import (
    Diagnostic,
    DiagnosticMonitor,
    RootHealthRounds,
    RoundsInputs,
)
from services.supervision.roster.composition import build_services, setting
from services.supervision.roster.field_values import flag, real, text, whole

_log = logging.getLogger(__name__)


# meiva:note
# An aggregate memory budget for one unit's whole process tree, in MiB; the swap cap
# belongs to it because a budget without one is not a budget.
@dataclass(frozen=True, slots=True)
class UnitMemoryCap:
    max_mb: int
    swap_max_mb: int = 0


# meiva:note
# One budget as configuration declares it: which unit gets it and the two numbers its
# transient scope carries. The unit id is part of the budget because the launch rewrite is
# keyed by it -- a budget that does not name its unit cannot be applied to anything.
@dataclass(frozen=True, slots=True)
class UnitBudget:
    unit_id: str
    max_mb: int
    swap_max_mb: int = 0


# meiva:note
# How long the assembly waits for one `systemd-run --user --scope` round trip before
# deciding the host cannot carry a scoped unit. It is a presence check, not a
# measurement: a machine that answers slowly is still a machine that answers.
_SCOPE_PROBE_TIMEOUT_S = 10.0


# meiva:note
# Standard systemd user-manager runtime root; parameterized so tests exercise real directories
# under `tmp_path` without patching filesystem lookups.
_USER_RUNTIME_ROOT = Path("/run/user")


# meiva:note
# The agent-host budget is opt-in per machine and only meaningful where cgroups exist.
# The probe runs once, at assembly, because the alternative failure is worse than having
# no cap: a unit wrapped in a launcher the host cannot run never starts at all, so a
# configuration mistake would take the whole agent plane down instead of bounding it.
# meiva:note
# systemd-run --user reaches the user manager through the bus that XDG_RUNTIME_DIR names.
# A daemonized root supervisor inherits no login session, so the variable is simply absent
# (ppid=1 once the launcher exits) -- and then both the capability probe and every scoped
# launch fail for a reason neither of them can report. On 2026-10-31 that disabled a
# configured 18432 MiB budget for a whole boot: the config reached the process, no OOM
# appeared in the journal, and only cgroup memory.max showed the unit running unbounded.
# meiva:note
# An absent path is not a guess worth making: returning None keeps "cannot scope" distinct
# from "cannot start at all", which is what inventing a directory that does not exist would
# trade it for.
def user_runtime_dir(
    environ: Mapping[str, str],
    *,
    uid: int,
    isdir: Callable[[str], bool],
    runtime_root: Path = _USER_RUNTIME_ROOT,
) -> str | None:
    current = environ.get("XDG_RUNTIME_DIR")
    if current:
        return current
    candidate = str(runtime_root / str(uid))
    return candidate if isdir(candidate) else None


# meiva:note
# Populate `XDG_RUNTIME_DIR` on the launch mapping when an existing user runtime dir is found.
# `Supervisor` snapshots its entry environment before `--wiring` loads, so `bind_memory_budget`
# installs any repaired value onto the supervisor (`set_launch_env`) and hands that same
# `supervisor.launch_environ()` mapping to the scope probe (`can_scope_units`): probe and
# spawned `systemd-run --user` wrapper see the identical environment and can never diverge.
def ensure_user_runtime_dir(
    environ: MutableMapping[str, str],
    *,
    uid: int,
    isdir: Callable[[str], bool],
    runtime_root: Path = _USER_RUNTIME_ROOT,
) -> str | None:
    resolved = user_runtime_dir(environ, uid=uid, isdir=isdir, runtime_root=runtime_root)
    if resolved is not None:
        environ["XDG_RUNTIME_DIR"] = resolved
    return resolved


# meiva:note
# Whether this host can scope a unit at all. Asked with the largest numbers configured and the
# exact base environment `Supervisor` will hand to `systemd-run --user`, so a refused value or
# unreachable user bus fails here at assembly rather than at the launch that starts the unit.
def can_scope_units(
    *,
    budgets: Sequence[UnitBudget],
    platform: str,
    env: Mapping[str, str],
    run: CommandRunner,
) -> bool:
    capped = [budget for budget in budgets if budget.max_mb > 0]
    if not capped or platform != "linux":
        return False
    return _systemd_user_scope_available(
        max(budget.max_mb for budget in capped),
        max(budget.swap_max_mb for budget in capped),
        env=env,
        run=run,
    )


# meiva:note
# Inputs are parameters, not ambient reads, so the decision is testable without patching: the
# caller answers `scope_available` and this function only decides what that answer means for
# a configured set of budgets.
def unit_memory_caps(
    *,
    budgets: Sequence[UnitBudget],
    platform: str,
    scope_available: bool,
) -> dict[str, UnitMemoryCap]:
    capped = [budget for budget in budgets if budget.max_mb > 0]
    if not capped:
        return {}
    described = ", ".join(f"{budget.unit_id}={budget.max_mb} MiB" for budget in capped)
    if platform != "linux":
        _log.info(
            "unit memory caps configured (%s) but this platform has no cgroups; "
            "the units run unbounded",
            described,
        )
        return {}
    if not scope_available:
        _log.warning(
            "unit memory caps configured (%s) but this systemd cannot carry the scope "
            "attributes they need; the units run unbounded rather than failing to start",
            described,
        )
        return {}
    return {
        budget.unit_id: UnitMemoryCap(max_mb=budget.max_mb, swap_max_mb=budget.swap_max_mb)
        for budget in capped
    }


# meiva:note
# The launch rewrite the root supervisor applies: only the unit carrying a budget is
# wrapped, and everything else keeps the manifest's argv byte for byte. Two limits go
# together -- a MemoryMax without a swap cap lets an over-budget tree page forever,
# which protects nothing and still fills the disk.
def memory_scoped_argv(
    unit_id: str, argv: Sequence[str], *, caps: Mapping[str, UnitMemoryCap]
) -> list[str]:
    cap = caps.get(unit_id)
    if cap is None:
        return list(argv)
    return [
        "systemd-run",
        "--user",
        "--scope",
        "--collect",
        "-p",
        f"MemoryMax={cap.max_mb}M",
        "-p",
        f"MemorySwapMax={cap.swap_max_mb}M",
        "--",
        *argv,
    ]


# meiva:note
# The probe carries the SAME attributes and environment the launch will use. A bare
# `systemd-run ... true` or an ambient-env probe proves neither that this systemd accepts
# MemoryMax/MemorySwapMax nor that the child's own launch environment reaches the user bus.
def _systemd_user_scope_available(
    max_mb: int,
    swap_max_mb: int,
    *,
    env: Mapping[str, str],
    run: CommandRunner,
) -> bool:
    try:
        done = run(
            [
                "systemd-run",
                "--user",
                "--scope",
                "--collect",
                "-p",
                f"MemoryMax={max_mb}M",
                "-p",
                f"MemorySwapMax={swap_max_mb}M",
                "true",
            ],
            capture_output=True,
            timeout=_SCOPE_PROBE_TIMEOUT_S,
            env=dict(env),
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


# meiva:note
# Whether any unit on this platform asks for a cgroup memory cap; gates the missing-runtime-dir
# error so a healthy macOS machine does not log a Linux user-bus complaint on every start.
def memory_cap_wanted(*, budgets: Sequence[UnitBudget], platform: str) -> bool:
    return platform == "linux" and any(budget.max_mb > 0 for budget in budgets)


# meiva:note
# The two budgets as configuration declares them. They stay separate knobs because the two
# trees hold different things: `agent-host` is one resident service process, while
# `exec-host` is where a turn's jobs run -- an arithmetic or model job can legitimately ask
# for many gigabytes there. One number for both would be wrong for one of them, and the
# wrong one is discovered by an OOM kill rather than by a config check.
def configured_memory_budgets() -> tuple[UnitBudget, ...]:
    return (
        UnitBudget(
            unit_id="agent-host",
            max_mb=settings.agent.agent_host_memory_max_mb,
            swap_max_mb=settings.agent.agent_host_memory_swap_max_mb,
        ),
        UnitBudget(
            unit_id="exec-host",
            max_mb=settings.agent.exec_host_memory_max_mb,
            swap_max_mb=settings.agent.exec_host_memory_swap_max_mb,
        ),
    )


# meiva:note
# Repair `XDG_RUNTIME_DIR` on the supervisor's launch environment before probing scope support,
# then probe `systemd-run --user` under `supervisor.launch_environ()` -- the exact environment
# `Supervisor._spawn` hands to the wrapped child. Mutating `os.environ` alone after `Supervisor`
# construction (2026-11-03 cold-start break) let the probe pass on the mutated process env while
# `Supervisor._spawn` launched `systemd-run --user` from its pre-wiring snapshot without
# `XDG_RUNTIME_DIR`, failing `Failed to connect to bus: No medium found` and retiring the cap.
def bind_memory_budget(
    supervisor: Supervisor,
    *,
    host: Host,
    budgets: Sequence[UnitBudget],
    retire_after_failures: int,
    runtime_root: Path = _USER_RUNTIME_ROOT,
) -> None:
    supervisor.set_rewrite_retire_after(retire_after_failures)
    launch_env = supervisor.launch_environ()
    had_runtime_dir = bool(launch_env.get("XDG_RUNTIME_DIR"))
    runtime_dir = ensure_user_runtime_dir(
        launch_env,
        uid=host.uid,
        isdir=os.path.isdir,
        runtime_root=runtime_root,
    )
    if runtime_dir is not None and not had_runtime_dir:
        supervisor.set_launch_env({"XDG_RUNTIME_DIR": runtime_dir})
    if runtime_dir is None and memory_cap_wanted(budgets=budgets, platform=host.platform.system):
        _log.error(
            "no user runtime dir: XDG_RUNTIME_DIR is unset and %s/%d does not exist; "
            "systemd-run --user cannot reach the user manager, so unit memory caps will not apply",
            runtime_root,
            host.uid,
        )
    caps = unit_memory_caps(
        budgets=budgets,
        platform=host.platform.system,
        scope_available=can_scope_units(
            budgets=budgets,
            platform=host.platform.system,
            env=supervisor.launch_environ(),
            run=host.run,
        ),
    )
    supervisor.set_argv_transform(
        (lambda unit_id, argv: memory_scoped_argv(unit_id, argv, caps=caps)) if caps else None
    )


# meiva:note
# The settings the host diagnostics decide on, read once here, where the root assembles them.
def probe_config_from_settings(*, home: Path) -> ProbeConfig:
    from base.sessions.helperproc import helper_client
    from services.desktop.browser.profile import profile_dir

    return ProbeConfig(
        home=home,
        helper_socket=helper_client(home=home).sock_path,
        redis_url=text(setting("redis_url"), "redis_url"),
        db_url=text(setting("db_url"), "db_url"),
        pgbouncer_enabled=flag(setting("pgbouncer_enabled"), "pgbouncer_enabled"),
        observability_url=text(setting("observability_url"), "observability_url"),
        observability_otlp_port=whole(
            setting("observability_otlp_port"), "observability_otlp_port"
        ),
        gateway_health_url=text(setting("gateway_health_url"), "gateway_health_url"),
        browser_cdp_port=whole(setting("browser_cdp_port"), "browser_cdp_port"),
        browser_profile=profile_dir(home),
        browser_reach_timeout_s=real(setting("browser_reach_timeout_s"), "browser_reach_timeout_s"),
        browser_reach_probe_interval_s=real(
            setting("browser_reach_probe_interval_s"), "browser_reach_probe_interval_s"
        ),
        loki_url=backend_urls(LgtmSettings.from_fields(get_field))["loki"],
        environment=MappingProxyType(inherited_process_env()),
    )


STATIC_PROBES: Mapping[str, str] = MappingProxyType({})
"""Spec-less unit id -> `"module:attribute"` probe reference.

v1 is empty on purpose: the host-policy / data-plane / native-stack class is
registered here when the wiring slice (W1.3) names its probe modules; the
reference is validated eagerly and imported lazily (the `register_ref`
contract).
"""


# meiva:note
# The root's one operator resource: accept a unit's current declared-input contents for this
# generation (task #6403). It rides the existing `resource` channel rather than a new wire verb,
# and returns what changed so the caller can print the exception it granted.
def admit_inputs_handler(supervisor: Supervisor) -> ResourceHandler:
    async def handle(payload: dict[str, object]) -> object:
        unit, caller = payload.get("unit"), payload.get("caller")
        if not isinstance(unit, str) or not unit:
            raise ValueError("admit-inputs needs a unit name")
        changed = supervisor.admit_inputs(unit, caller=caller if isinstance(caller, str) else "")
        return {"unit": unit, "changed": [list(item) for item in changed]}

    return handle


def build_wiring(
    context: WiringContext,
    *,
    config: ConfigBoot | None = None,
    diagnostics: Sequence[Diagnostic] | None = None,
    probe_config: ProbeConfig | None = None,
    services: Sequence[ProbeSource] | None = None,
    static_probes: Mapping[str, str] = STATIC_PROBES,
) -> list[WiringParticipant]:
    """The reference participant set: health monitor + tree self-check."""
    from base.host.runtime_interpreter import capture_loaded_runtime
    from base.paths import meiva_home

    home = meiva_home().resolve(strict=True)
    identity = capture_loaded_runtime()
    if Path.cwd().resolve() != Path(identity.cwd):
        raise RuntimeError("root cwd differs from its loaded runtime")
    context.supervisor.bind_runtime(identity, home=home)
    image = LoadedCommit.capture(Path(identity.code_root))
    if config is None:
        config = ConfigBoot()
        config.boot()
    host = Host.system(environ=config.process_environment())
    bind_memory_budget(
        context.supervisor,
        host=host,
        budgets=configured_memory_budgets(),
        retire_after_failures=settings.agent.rewrite_retire_after_failures,
    )
    machine = ConfiguredMachineIdentity(config)
    clock = context.clock
    version = CodeVersion(image)
    gate = ProcessDbGate(version=version.get, process="meiva-root", end_process=hard_exit)

    def database() -> Database:
        return Database(
            db_config_from_boot(config),
            gate=gate,
            direct_url=ClusterDirectUrl(machine.host, home=home),
        )

    clients = ClientSet(
        database=database,
        pipeline_factory=lambda: build_pipeline(
            database=database,
            machine=machine,
            home=home,
            otlp_enabled=lambda: config.view.observability.telemetry_otlp_enabled,
            otlp_endpoint=lambda: config.view.observability.telemetry_otlp_endpoint,
        ),
    )
    registry = ProbeRegistry()
    requested = {unit.id for unit in context.registry.units}
    raw_specs: Sequence[ProbeSource] = build_services(home=home) if services is None else services
    registry.register_specs(spec for spec in raw_specs if spec.session in requested)
    for unit_id, ref in static_probes.items():
        registry.register_ref(unit_id, ref)
    missing = requested - set(registry.unit_ids())
    if missing:
        raise ProbeError(f"root units lack readiness probes: {', '.join(sorted(missing))}")
    startup_graces = {
        unit_id: SERVICE_READY_TIMEOUT_S
        if unit_id in CRITICAL_SERVICE_SESSIONS
        else NON_CRITICAL_SERVICE_READY_TIMEOUT_S
        for unit_id in requested
    }
    resolved_diagnostics = (
        build_diagnostics(
            requested,
            database=database,
            machine=machine,
            config=probe_config_from_settings(home=home) if probe_config is None else probe_config,
            clock=clock,
            macos=Platform.current().system == "darwin",
            launchd=LaunchdFacts.of(host, user_home=Path.home()),
            run=host.run,
            root=context.supervisor.root_identity(),
            # meiva:note
            # The venv probe runs its legs outside this checkout: the OS temp directory is the one
            # directory the root knows that is never a working tree.
            probe_cwd=Path(tempfile.gettempdir()),
        )
        if diagnostics is None
        else diagnostics
    )
    context.resource_handlers["admit-inputs"] = admit_inputs_handler(context.supervisor)
    return assemble(
        context,
        registry,
        health_config=HealthConfig(lease_grace_s=settings.daemon.health_restart_lease_grace_s),
        db_recovery_probe=database_recovery_probe(database),
        lease_probe=live_lease_probe(database, machine.name()),
        rounds=RoundsInputs(
            diagnostics=DiagnosticMonitor(resolved_diagnostics, clock=clock),
            clients=clients,
            image=image,
            machine=machine,
            clock=clock,
            home=home,
        ),
        startup_graces=startup_graces,
    )


def assemble(
    context: WiringContext,
    registry: ProbeRegistry,
    *,
    health_config: HealthConfig | None = None,
    selfcheck_config: SelfCheckConfig | None = None,
    rounds: RoundsInputs | None = None,
    startup_graces: Mapping[str, float] | None = None,
    db_recovery_probe: Probe | None = None,
    lease_probe: LeaseProbe | None = None,
) -> list[WiringParticipant]:
    """Build the two monitors over `registry` and attach their status surfaces."""
    monitor = HealthMonitor(
        context.supervisor,
        registry,
        config=health_config,
        tasks=context.participant_tasks,
        startup_graces=startup_graces,
        db_recovery_probe=db_recovery_probe,
        lease_probe=lease_probe,
        clock=context.clock,
    )
    check = TreeSelfCheck(
        context.supervisor,
        timers=context.clock,
        config=selfcheck_config,
        tasks=context.participant_tasks,
    )
    if rounds is not None:
        health_rounds = RootHealthRounds(
            monitor,
            rounds.diagnostics,
            clients=rounds.clients,
            machine=rounds.machine,
            image=rounds.image,
            clock=rounds.clock,
            home=rounds.home,
            tasks=context.participant_tasks,
        )
        context.supervisor.attach_health(health_rounds)
        context.supervisor.attach_metrics(check)
        return [health_rounds, check]
    context.supervisor.attach_health(monitor)
    context.supervisor.attach_metrics(check)
    return [monitor, check]

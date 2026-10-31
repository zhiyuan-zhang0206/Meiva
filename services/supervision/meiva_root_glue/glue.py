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
from services.supervision.meiva_root.wiring import WiringContext, WiringParticipant
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
from services.supervision.roster.composition import build_services

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
    environ: Mapping[str, str], *, uid: int, isdir: Callable[[str], bool]
) -> str | None:
    current = environ.get("XDG_RUNTIME_DIR")
    if current:
        return current
    candidate = f"/run/user/{uid}"
    return candidate if isdir(candidate) else None


# meiva:note
# Writing it onto the process is the point, not a convenience: the launch rewrite hands
# `systemd-run --user` to the supervisor, which inherits this environment. Repairing the
# probe alone would leave that second hop failing exactly as before.
def ensure_user_runtime_dir(
    environ: MutableMapping[str, str], *, uid: int, isdir: Callable[[str], bool]
) -> str | None:
    resolved = user_runtime_dir(environ, uid=uid, isdir=isdir)
    if resolved is not None:
        environ["XDG_RUNTIME_DIR"] = resolved
    return resolved


# meiva:note
# Whether this host can scope a unit at all. Asked with the largest numbers configured, so a
# value the host refuses (rather than the mechanism itself) fails at assembly rather than at
# the launch that would have started the unit. One probe for the whole set: it answers for
# the mechanism, not for a particular unit.
def can_scope_units(*, budgets: Sequence[UnitBudget], platform: str) -> bool:
    capped = [budget for budget in budgets if budget.max_mb > 0]
    if not capped or platform != "linux":
        return False
    return _systemd_user_scope_available(
        max(budget.max_mb for budget in capped),
        max(budget.swap_max_mb for budget in capped),
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
# The probe carries the SAME attributes the launch will use. A bare `systemd-run ... true`
# proves the binary exists, not that this systemd accepts MemoryMax or MemorySwapMax -- and
# a refused attribute is exactly how the launch would fail.
def _systemd_user_scope_available(max_mb: int, swap_max_mb: int) -> bool:
    try:
        done = subprocess.run(  # noqa: S603
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
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


# meiva:note
# The one place the memory budget is decided: config, platform and scope support are read
# here and handed on as an opaque launch rewrite, so the supervisor stays free of platform
# names and ambient config.
#
# It takes `host` because the decision needs the user id, and `Host` is the door that already
# carries it. A daemonized supervisor inherits no login session, so the user-bus variable has
# to be restored before anything asks whether scoping is possible -- and restoring it in the
# process environment is the point, because the later `systemd-run --user` launch inherits it
# too. Repairing only the probe would leave that second hop failing for the same reason.
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
# The repair runs unconditionally -- writing a runtime dir that already exists costs nothing
# and the launch below may need it on platforms that never cap. The complaint does not: an
# alert on a healthy macOS developer machine, naming a Linux mechanism that does not apply
# there, is how an alert becomes noise.
def bind_memory_budget(supervisor: Supervisor, *, host: Host) -> None:
    # meiva:note
    # The retirement policy is bound here, with the rewrite it governs: the count is what bounds
    # the restart loop a rewrite can cause, and it is configuration because "how many transient
    # wrapper failures is this host allowed" is an operating decision, not a code constant.
    supervisor.set_rewrite_retire_after(settings.agent.rewrite_retire_after_failures)
    runtime_dir = ensure_user_runtime_dir(os.environ, uid=host.uid, isdir=os.path.isdir)
    budgets = configured_memory_budgets()
    if runtime_dir is None and memory_cap_wanted(budgets=budgets, platform=host.platform.system):
        _log.error(
            "no user runtime dir: XDG_RUNTIME_DIR is unset and /run/user/%d does not exist; "
            "systemd-run --user cannot reach the user manager, so unit memory caps will not apply",
            host.uid,
        )
    caps = unit_memory_caps(
        budgets=budgets,
        platform=host.platform.system,
        scope_available=can_scope_units(budgets=budgets, platform=host.platform.system),
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
        redis_url=str(get_field("redis_url")),
        db_url=str(get_field("db_url")),
        pgbouncer_enabled=bool(get_field("pgbouncer_enabled")),
        observability_url=str(get_field("observability_url")),
        observability_otlp_port=int(get_field("observability_otlp_port")),
        gateway_health_url=str(get_field("gateway_health_url")),
        browser_cdp_port=int(get_field("browser_cdp_port")),
        browser_profile=profile_dir(home),
        browser_reach_timeout_s=float(get_field("browser_reach_timeout_s")),
        browser_reach_probe_interval_s=float(get_field("browser_reach_probe_interval_s")),
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
    bind_memory_budget(context.supervisor, host=host)
    machine = ConfiguredMachineIdentity(config)
    clock = context.clock
    version = CodeVersion(image)
    gate = ProcessDbGate(version=version.get, process="meiva-root")

    def database() -> Database:
        return Database(
            db_config_from_boot(config),
            gate=gate,
            direct_url=ClusterDirectUrl(machine.host, home=home),
        )

    clients = ClientSet(
        database=database,
        pipeline_factory=lambda: build_pipeline(database=database, machine=machine, home=home),
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
        )
        if diagnostics is None
        else diagnostics
    )
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

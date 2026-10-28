"""Bind actual root units to readiness, diagnostics, and tree self-check.

Service recovery belongs to HealthMonitor; host diagnostics have no launch
verbs. One round coordinator publishes freshness after both parts complete.
Native data-plane custody and the macOS parent helper remain explicit external
boundaries; diagnostics never repair them from inside the service subtree.
"""

from __future__ import annotations

import logging
import subprocess
from collections.abc import Mapping, Sequence
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
from base.telemetry.lgtm_local import backend_urls
from services.desktop.permissions_helper.launchd_job import LaunchdFacts
from services.supervision.meiva_root.health import HealthConfig, HealthMonitor, LeaseProbe
from services.supervision.meiva_root.probes import Probe, ProbeError, ProbeRegistry, ProbeSource
from services.supervision.meiva_root.selfcheck import SelfCheckConfig, TreeSelfCheck
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
# How long the assembly waits for one `systemd-run --user --scope` round trip before
# deciding the host cannot carry a scoped unit. It is a presence check, not a
# measurement: a machine that answers slowly is still a machine that answers.
_SCOPE_PROBE_TIMEOUT_S = 10.0


# meiva:note
# The agent-host budget is opt-in per machine and only meaningful where cgroups exist.
# The probe runs once, at assembly, because the alternative failure is worse than having
# no cap: a unit wrapped in a launcher the host cannot run never starts at all, so a
# configuration mistake would take the whole agent plane down instead of bounding it.
# Inputs are parameters, not ambient reads, so the decision is testable without patching.
def agent_host_memory_caps(
    *,
    max_mb: int,
    swap_max_mb: int,
    platform: str,
    scope_available: bool | None = None,
) -> dict[str, UnitMemoryCap]:
    if max_mb <= 0:
        return {}
    if platform != "linux":
        _log.info(
            "agent-host memory cap configured (%d MiB) but this platform has no cgroups; "
            "the unit runs unbounded",
            max_mb,
        )
        return {}
    supported = (
        _systemd_user_scope_available(max_mb, swap_max_mb)
        if scope_available is None
        else scope_available
    )
    if not supported:
        _log.warning(
            "agent-host memory cap configured (%d MiB) but this systemd cannot carry the "
            "scope attributes it needs; the unit runs unbounded rather than failing to start",
            max_mb,
        )
        return {}
    return {"agent-host": UnitMemoryCap(max_mb=max_mb, swap_max_mb=swap_max_mb)}


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
        loki_url=backend_urls()["loki"],
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
    # meiva:note
    # The one place the memory budget is decided: config, platform and scope support are
    # read here and handed on as an opaque launch rewrite, so the supervisor stays free
    # of platform names and ambient config.
    caps = agent_host_memory_caps(
        max_mb=settings.agent.agent_host_memory_max_mb,
        swap_max_mb=settings.agent.agent_host_memory_swap_max_mb,
        platform=Platform.current().system,
    )
    context.supervisor.set_argv_transform(
        (lambda unit_id, argv: memory_scoped_argv(unit_id, argv, caps=caps)) if caps else None
    )
    image = LoadedCommit.capture(Path(identity.code_root))
    if config is None:
        config = ConfigBoot()
        config.boot()
    host = Host.system(environ=config.process_environment())
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

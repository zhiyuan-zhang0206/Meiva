"""Bind actual root units to readiness, diagnostics, and tree self-check.

Service recovery belongs to HealthMonitor; host diagnostics have no launch
verbs. One round coordinator publishes freshness after both parts complete.
Native data-plane custody and the macOS parent helper remain explicit external
boundaries; diagnostics never repair them from inside the service subtree.
"""

from __future__ import annotations

import logging
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from base.agents.context.clients import ClientSet
from base.cluster.dataplane.direct_url import ClusterDirectUrl
from base.cluster.machine.boot import machine_from_boot
from base.config import ConfigBoot, settings
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.db.config import db_config_from_boot
from base.deploy.progress_timeout import (
    CRITICAL_SERVICE_SESSIONS,
    NON_CRITICAL_SERVICE_READY_TIMEOUT_S,
    SERVICE_READY_TIMEOUT_S,
)
from base.native_process.code_version import CodeVersion
from base.native_process.loaded_commit import LoadedCommit
from base.telemetry import build_pipeline
from services.supervision.meiva_root.health import HealthConfig, HealthMonitor
from services.supervision.meiva_root.probes import Probe, ProbeError, ProbeRegistry
from services.supervision.meiva_root.selfcheck import SelfCheckConfig, TreeSelfCheck
from services.supervision.meiva_root.wiring import WiringContext, WiringParticipant
from services.supervision.meiva_root_glue.diagnostic_probes import (
    build_diagnostics,
    database_recovery_probe,
)
from services.supervision.meiva_root_glue.diagnostics import (
    DiagnosticMonitor,
    RootHealthRounds,
)
from services.supervision.roster import build_services

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
    scope_available: Callable[[], bool],
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
    if not scope_available():
        _log.warning(
            "agent-host memory cap configured (%d MiB) but systemd cannot create a user "
            "scope here; the unit runs unbounded rather than failing to start",
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


def _systemd_user_scope_available() -> bool:
    try:
        done = subprocess.run(
            ["systemd-run", "--user", "--scope", "--collect", "true"],
            capture_output=True,
            timeout=_SCOPE_PROBE_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


STATIC_PROBES: Mapping[str, str] = MappingProxyType({})
"""Spec-less unit id -> `"module:attribute"` probe reference.

v1 is empty on purpose: the host-policy / data-plane / native-stack class is
registered here when the wiring slice (W1.3) names its probe modules; the
reference is validated eagerly and imported lazily (the `register_ref`
contract).
"""


def build_wiring(context: WiringContext) -> list[WiringParticipant]:
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
        platform=sys.platform,
        scope_available=_systemd_user_scope_available,
    )
    context.supervisor.set_argv_transform(
        (lambda unit_id, argv: memory_scoped_argv(unit_id, argv, caps=caps)) if caps else None
    )
    image = LoadedCommit.capture(Path(identity.code_root))
    config = ConfigBoot()
    config.boot()
    machine = machine_from_boot(config)
    version = CodeVersion(image)
    gate = ProcessDbGate(version=version.get, process="meiva-root")

    def database() -> Database:
        return Database(
            db_config_from_boot(config),
            gate=gate,
            direct_url=ClusterDirectUrl(machine.host),
        )

    clients = ClientSet(
        database=database,
        pipeline_factory=lambda: build_pipeline(database=database, machine=machine, home=home),
    )
    registry = ProbeRegistry()
    requested = {unit.id for unit in context.registry.units}
    registry.register_specs(spec for spec in build_services() if spec.session in requested)
    for unit_id, ref in STATIC_PROBES.items():
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
    return assemble(
        context,
        registry,
        db_recovery_probe=database_recovery_probe(database),
        rounds_factory=lambda monitor: RootHealthRounds(
            monitor,
            DiagnosticMonitor(build_diagnostics(requested, database=database, machine=machine)),
            clients=clients,
            machine=machine,
            image=image,
            tasks=context.participant_tasks,
        ),
        startup_graces=startup_graces,
    )


def assemble(
    context: WiringContext,
    registry: ProbeRegistry,
    *,
    health_config: HealthConfig | None = None,
    selfcheck_config: SelfCheckConfig | None = None,
    rounds_factory: Callable[[HealthMonitor], RootHealthRounds] | None = None,
    startup_graces: Mapping[str, float] | None = None,
    db_recovery_probe: Probe | None = None,
) -> list[WiringParticipant]:
    """Build the two monitors over `registry` and attach their status surfaces."""
    monitor = HealthMonitor(
        context.supervisor,
        registry,
        config=health_config,
        tasks=context.participant_tasks,
        startup_graces=startup_graces,
        db_recovery_probe=db_recovery_probe,
    )
    check = TreeSelfCheck(
        context.supervisor, config=selfcheck_config, tasks=context.participant_tasks
    )
    if rounds_factory is not None:
        rounds = rounds_factory(monitor)
        context.supervisor.attach_health(rounds)
        context.supervisor.attach_metrics(check)
        return [rounds, check]
    context.supervisor.attach_health(monitor)
    context.supervisor.attach_metrics(check)
    return [monitor, check]

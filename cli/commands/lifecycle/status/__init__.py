# meiva:note
# `meiva status` — one-screen view of services, infra, host relays, and cron.
#
# The local session/infra probes are the bootstrap-exemption supplement: they work even
# when the gateway is down (e.g. mid-restart), and the gateway can only see itself. So
# `meiva status` shows both — the local view always, the gateway's own cluster-status
# snapshot (GET `/api/cluster/status`) additionally — rather than delegating wholesale.
#
# The command is a shell around a plan. `cmd_status` reads a few facts about this host,
# `plan.plan_status` decides which sections print and in which order, and each step here
# performs the one probe or reading it names and prints what the module that owns the
# wording (`roster`, `render`, `gateway`) returns for it. The service rows, the data-plane
# view and the relay PING come from their owners' own probes (`print_service_row`,
# `print_data_plane_status`, `print_redis_bridge_status`) and are printed as they run.

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import httpx

from base.agents.context.clients import DatabaseFactory
from base.cluster import session_name
from base.cluster.machine import (
    GatewayApiBaseMissing,
    GatewayApiTokenMissing,
    MachineRoleInvalid,
    MachineRoleMissing,
    MachineRoles,
    format_capabilities,
)
from base.cluster.machine.boot import gateway_access_from_boot
from base.config import ConfigBoot
from base.deploy.git import cluster_drift
from base.host.resource_sample import resource_sample
from base.paths import root_manifests_path
from cli.commands._repo import repo_root, roles_or_none
from cli.commands.cluster.control import fetch_gateway_cluster_status
from cli.commands.converge import incomplete
from cli.commands.converge.redis_bridge import print_redis_bridge_status
from cli.commands.data_plane.bringup import print_data_plane_status
from cli.commands.data_plane.config import data_plane_config_from_boot
from cli.commands.lifecycle import root_driver
from cli.commands.lifecycle.root_control.snapshot import root_units
from cli.commands.lifecycle.status.gateway import (
    GatewayCredentialMissing,
    GatewayOutcome,
    GatewayReport,
    GatewayUnreachable,
    GatewayUnresolved,
    gateway_status_lines,
)
from cli.commands.lifecycle.status.plan import (
    PrintLines,
    PrintServiceRows,
    ProbeDataPlane,
    ProbeLgtm,
    ProbeRedisBridge,
    ReadGateway,
    ReadHostResources,
    ReadRelease,
    ReadSourceDrift,
    ReportConverge,
    StatusFacts,
    StatusStep,
    plan_status,
)
from cli.commands.lifecycle.status.render import (
    HostReading,
    HostReadingFailed,
    converge_lines,
    host_resource_lines,
    release_line,
    source_drift_lines,
)
from cli.commands.lifecycle.status.roster import RosterEntry, ServiceTable
from cli.commands.observability.lgtm import is_lgtm_host, print_lgtm_status
from cli.commands.probe import print_service_row
from cli.database.runtime import CommandRuntime
from ops.cluster_status import ClusterStatus
from services.supervision.meiva_root_glue.manifests import launched_services
from services.supervision.roster import build_services
from services.supervision.roster.service_spec import ServiceSpec
from services.supervision.roster.spec import services_for_capabilities_annotated


def cmd_status(*, database_factory: DatabaseFactory, command: CommandRuntime) -> int:
    repo = repo_root()
    roles = roles_or_none(machine=command.machine)
    roster = _roster(roles, command)
    facts = StatusFacts(
        repo=str(repo),
        roles=roles,
        roster=tuple(
            RosterEntry(spec.session, session_name(spec.session), reason) for spec, reason in roster
        ),
        launched=launched_services(root_manifests_path(command.home)),
        lgtm_host=is_lgtm_host(machine=command.machine),
    )
    specs = tuple(spec for spec, _reason in roster)
    for step in plan_status(facts):
        _execute(step, database_factory=database_factory, command=command, specs=specs)
    return 0


def _roster(
    roles: MachineRoles | None, command: CommandRuntime
) -> tuple[tuple[ServiceSpec, str | None], ...]:
    # meiva:note
    # Unknown role (None): show the full list, ungated (no reasons). Otherwise this host's
    # role roster WITH each gated-out service's reason; gateway daemons on a runner-only
    # host stay filtered (they are not in the role union).
    if roles is None:
        return tuple((spec, None) for spec in build_services())
    return services_for_capabilities_annotated(roles, machine=command.machine, host=command.host)


def _execute(
    step: StatusStep,
    *,
    database_factory: DatabaseFactory,
    command: CommandRuntime,
    specs: tuple[ServiceSpec, ...],
) -> None:
    match step:
        case PrintLines(lines):
            _emit(lines)
        case ReportConverge():
            print_converge_section(command.home)
        case PrintServiceRows(table):
            _print_service_rows(table, specs, root_tree_units(command.home))
        case ProbeDataPlane():
            print_data_plane_status(
                data_plane_config_from_boot(command.config),
                home=command.home,
                database_factory=database_factory,
            )
        case ReadHostResources():
            _emit(host_resource_lines(_read_host_resources()))
        case ProbeLgtm():
            print_lgtm_status()
        case ProbeRedisBridge():
            print_redis_bridge_status(machine=command.machine, config=command.config)
        case ReadSourceDrift():
            _emit(source_drift_lines(cluster_drift.prod_source_branch_drift()))
        case ReadRelease(repo):
            _emit((release_line(repo, cluster_drift.checkout_head_sha(Path(repo))),))
        case ReadGateway():
            _emit(gateway_status_lines(read_gateway_outcome(command.config)))


def _emit(lines: tuple[str, ...]) -> None:
    for line in lines:
        print(line)


def print_converge_section(home: Path) -> None:
    recorded = incomplete.read_incomplete(home)
    _emit(converge_lines(None if recorded is None else recorded.summary()))


# meiva:note
# Read one root snapshot; an unreachable or unreadable root claims no running services.
#
# The root is the only service owner. Liveness is never inferred from old records. A root
# that answers with a status the commands cannot read is named on stderr and the table still
# prints: `meiva status` is the command to run on a host whose root is misbehaving.
def root_tree_units(home: Path) -> dict[str, dict[str, Any]]:
    try:
        snapshot = root_driver.root_status(root_driver.root_client(home))
    except root_driver.RootDriverError as exc:
        print(f"  ! {exc}", file=sys.stderr)
        return {}
    if snapshot is None:
        return {}
    return {unit_id: dict(row) for unit_id, row in root_units(snapshot).items()}


def _print_service_rows(
    table: ServiceTable, specs: tuple[ServiceSpec, ...], root_units: dict[str, dict[str, Any]]
) -> None:
    print(table.header)
    print(table.rule)
    for spec, note in zip(specs, table.notes, strict=True):
        print_service_row(spec, table.name_width, note, root_units=root_units)


# meiva:note
# One live reading, or the reason there is none.
#
# A read failure is a value, not an abort: the reading is one section of `meiva status`,
# and a host without psutil must still get the service table and the views below it.
def _read_host_resources() -> HostReading | HostReadingFailed:
    try:
        sample = resource_sample()
    # meiva:note
    # psutil may be absent; the rest of status still prints
    except Exception as exc:
        return HostReadingFailed(str(exc))
    return HostReading(
        cpu_pct=sample.cpu_pct,
        mem_pct=sample.mem_pct,
        mem_used_gb=sample.mem_used_gb,
        mem_total_gb=sample.mem_total_gb,
        disk_pct=sample.disk_pct,
        disk_used_gb=sample.disk_used_gb,
        disk_total_gb=sample.disk_total_gb,
    )


# meiva:note
# Fetch the gateway's own cluster-status snapshot, or the reason there is none.
#
# A gateway that is down or unreachable is itself a status signal here, so the failure
# is a value that prints inline instead of aborting `meiva status` — the local probes
# above already ran and are the bootstrap-exemption supplement.
def read_gateway_outcome(config: ConfigBoot) -> GatewayOutcome:
    try:
        body = fetch_gateway_cluster_status(gateway_access_from_boot(config))
    except httpx.HTTPError as exc:
        return GatewayUnreachable(str(exc))
    except json.JSONDecodeError as exc:
        # meiva:note
        # Something answered the status request with a body that is not JSON (a proxy's page,
        # a gateway of another version): the gateway did not answer with a status body.
        return GatewayUnreachable(f"the answer is not JSON ({exc})")
    except GatewayApiTokenMissing as exc:
        # meiva:note
        # The credential policy refuses to present a blank bearer. The command stays a
        # diagnostic: it says what is missing and the sections around it still print.
        return GatewayCredentialMissing(str(exc))
    except (GatewayApiBaseMissing, MachineRoleMissing, MachineRoleInvalid) as exc:
        # meiva:note
        # Can't even form the gateway URL (unset gateway URL / role): status is a diagnostic
        # that must still run on a misconfigured host.
        return GatewayUnresolved(str(exc))
    return gateway_report_from_body(body)


# meiva:note
# Read the `GET /api/cluster/status` body; a body that is not a ClusterStatus raises.
def gateway_report_from_body(body: dict[str, object]) -> GatewayReport:
    status = ClusterStatus.model_validate(body)
    return GatewayReport(
        machine_name=status.machine_name,
        serves=format_capabilities(
            status.serve_gateway, status.serve_agent_runner, status.serve_observability_station
        ),
        paused=status.paused,
        paused_reason=status.paused_reason,
    )

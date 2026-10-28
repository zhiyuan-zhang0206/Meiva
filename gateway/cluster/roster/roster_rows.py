# meiva:note
# Row shapers for the roster and the status panel (gateway/cluster/status.py).
#
# Every row is a pure function of a machines-table row and the facts read for it: no probe
# state, no backoff, no cluster-global markers. The abnormal states (reachable-unknown,
# offline, identity mismatch), a row rendered from a host's own `status_probe` answer, the
# gateway's own lightweight row and a daemon's service line all live here, so the row contract
# is in one place and the shell that reads facts stays thin.

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TypedDict

from base.api_contracts.status import MachineStatus, PausedReason, SchemaMismatchStatus
from base.host.resource.reading import ResourceSample
from gateway.cluster.schemas import ServiceItem

__all__ = [
    "LocalFacts",
    "ProbedFacts",
    "RosterRowSpec",
    "identity_mismatch_status",
    "local_status",
    "offline_status",
    "reachable_unknown_status",
    "reported_status",
    "service_item",
]


# meiva:note
# One machines-table row, in the column order the roster reads it.
@dataclass(frozen=True)
class RosterRowSpec:
    name: str
    gateway_url: str | None
    role: list[str]
    up_since_at: datetime
    description: str | None
    stopped_at: datetime | None
    is_staging: bool = False


# meiva:note
# The part of a host's own `status_probe` answer (`ClusterStatus`) a roster row shows.
#
# The shell copies these fields from the validated answer (`roster_probe.probed_facts`), so the
# row shaper stays free of the probe's models.
@dataclass(frozen=True)
class ProbedFacts:
    machine_name: str
    paused: bool
    paused_reason: PausedReason | None
    head_sha: str | None
    running_sha: str | None
    schema_mismatch: SchemaMismatchStatus | None
    shell_count: int
    agent_host_online: bool | None
    supervisor_online: bool | None
    agent_count: int
    session_count: int
    agent_groups: list[dict[str, object]]
    resource: ResourceSample | None


# meiva:note
# What the gateway's own machine reports about itself when it runs no ops server.
@dataclass(frozen=True)
class LocalFacts:
    paused_reason: PausedReason | None
    head_sha: str | None
    running_sha: str | None
    schema_mismatch: SchemaMismatchStatus | None
    resource: ResourceSample | None


class _Capabilities(TypedDict):
    serve_gateway: bool
    serve_agent_runner: bool
    serve_observability_station: bool


def _capabilities(row: RosterRowSpec) -> _Capabilities:
    return {
        "serve_gateway": "gateway" in row.role,
        "serve_agent_runner": "agent-runner" in row.role,
        "serve_observability_station": "observability-station" in row.role,
    }


# meiva:note
# The MachineStatus row for a reachable host whose status is unknown.
#
# The documented online=True + paused=None abnormal state (see MachineStatus):
# the probe got through, but either the operation failed or its response did
# not match the status_probe contract. A loud unknown state — never disguised
# as a determinate paused verdict, and distinct from offline (the probe never
# reached the host at all).
def reachable_unknown_status(row: RosterRowSpec) -> MachineStatus:
    return MachineStatus(
        name=row.name,
        **_capabilities(row),
        gateway_url=row.gateway_url or "",
        up_since_at=row.up_since_at,
        online=True,
        paused=None,
        description=row.description,
        stopped_at=row.stopped_at,
        is_staging=row.is_staging,
        head_sha=None,
    )


# meiva:note
# The MachineStatus row for a host we could not get a usable probe from.
def offline_status(row: RosterRowSpec) -> MachineStatus:
    return MachineStatus(
        name=row.name,
        **_capabilities(row),
        gateway_url=row.gateway_url or "",
        up_since_at=row.up_since_at,
        online=False,
        paused=None,
        description=row.description,
        stopped_at=row.stopped_at,
        is_staging=row.is_staging,
        head_sha=None,
    )


# meiva:note
# The MachineStatus row for a host whose ops server answered under a DIFFERENT
# machine_name than the row we targeted.
#
# A loud, distinct state — `online=False` + `identity_mismatch=True` — so a
# loopback/misregistered gateway_url that makes the gateway dial itself (and
# answer 200 under its own name) can no longer masquerade as the target host
# online. Structurally impossible to render as a false green.
def identity_mismatch_status(row: RosterRowSpec) -> MachineStatus:
    return MachineStatus(
        name=row.name,
        **_capabilities(row),
        gateway_url=row.gateway_url or "",
        up_since_at=row.up_since_at,
        online=False,
        paused=None,
        description=row.description,
        stopped_at=row.stopped_at,
        is_staging=row.is_staging,
        head_sha=None,
        identity_mismatch=True,
    )


# meiva:note
# Render one machine's validated `status_probe` answer as a roster row.
#
# Identity echo check: the ops server self-reports its machine_name in every
# status_probe response. If the responder is NOT the host we targeted, the
# gateway_url pointed at the wrong box (a loopback/misregistered row makes the
# gateway dial itself and answer under its own name). That is never rendered as
# the target online: a loud identity-mismatch row instead.
def reported_status(
    row: RosterRowSpec, probed: ProbedFacts, *, observed_at: datetime | None
) -> MachineStatus:
    if probed.machine_name != row.name:
        return identity_mismatch_status(row)
    return MachineStatus(
        name=row.name,
        **_capabilities(row),
        gateway_url=row.gateway_url or "",
        up_since_at=row.up_since_at,
        online=True,
        paused=probed.paused,
        paused_reason=probed.paused_reason,
        description=row.description,
        stopped_at=row.stopped_at,
        is_staging=row.is_staging,
        head_sha=probed.head_sha,
        running_sha=probed.running_sha,
        schema_mismatch=probed.schema_mismatch,
        shell_count=probed.shell_count,
        agent_host_online=probed.agent_host_online,
        supervisor_online=probed.supervisor_online,
        agent_count=probed.agent_count,
        session_count=probed.session_count,
        agent_groups=probed.agent_groups,
        resource=probed.resource,
        observed_at=observed_at,
    )


# meiva:note
# The row for the gateway's own machine when it has no agent-runner capability.
#
# It runs no ops server, so its row is a lightweight local read: the serving gate, the
# checkout's HEAD, the process's frozen commit and a resource sample, with no
# session/pidfile probes (shell/daemon liveness is agent-runner data a pure gateway does
# not have).
def local_status(row: RosterRowSpec, facts: LocalFacts) -> MachineStatus:
    return MachineStatus(
        name=row.name,
        **_capabilities(row),
        gateway_url=row.gateway_url or "",
        up_since_at=row.up_since_at,
        online=True,
        paused=facts.paused_reason is not None,
        paused_reason=facts.paused_reason,
        description=row.description,
        stopped_at=row.stopped_at,
        is_staging=row.is_staging,
        head_sha=facts.head_sha,
        running_sha=facts.running_sha,
        schema_mismatch=facts.schema_mismatch,
        resource=facts.resource,
    )


# meiva:note
# One gateway-only daemon's line in the services block.
def service_item(name: str, label: str, *, alive: bool, pid: int | None) -> ServiceItem:
    return ServiceItem(
        name=name,
        label=label,
        online=alive,
        pid=pid,
        detail=None
        if alive
        else ("pidfile exists but process is dead" if pid else "pidfile not found"),
    )

"""MachineStatus row shapers for the roster fan-out (gateway/cluster/status.py).

The three abnormal-state rows — reachable-unknown, offline, identity mismatch —
are pure functions of a machines-table row: no probe state, no backoff, no
cluster-global markers.
Split out of status.py so the roster module stays under the 800-line budget
while the row contract lives in one place."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from base.api_contracts.status import MachineStatus


@dataclass(frozen=True)
class RosterRowSpec:
    name: str
    role: list[str]
    gateway_url: str | None
    up_since_at: datetime
    description: str | None
    stopped_at: datetime | None
    is_staging: bool = False


def reachable_unknown_status(row: RosterRowSpec) -> MachineStatus:
    """The MachineStatus row for a reachable host whose status is unknown.

    The documented online=True + paused=None abnormal state (see MachineStatus):
    the probe got through, but either the operation failed or its response did
    not match the status_probe contract. A loud unknown state — never disguised
    as a determinate paused verdict, and distinct from offline (the probe never
    reached the host at all).
    """
    return MachineStatus(
        name=row.name,
        serve_gateway="gateway" in row.role,
        serve_agent_runner="agent-runner" in row.role,
        serve_observability_station="observability-station" in row.role,
        gateway_url=row.gateway_url or "",
        up_since_at=row.up_since_at,
        online=True,
        paused=None,
        description=row.description,
        stopped_at=row.stopped_at,
        is_staging=row.is_staging,
        head_sha=None,
    )


def offline_status(row: RosterRowSpec) -> MachineStatus:
    """The MachineStatus row for a host we could not get a usable probe from."""
    return MachineStatus(
        name=row.name,
        serve_gateway="gateway" in row.role,
        serve_agent_runner="agent-runner" in row.role,
        serve_observability_station="observability-station" in row.role,
        gateway_url=row.gateway_url or "",
        up_since_at=row.up_since_at,
        online=False,
        paused=None,
        description=row.description,
        stopped_at=row.stopped_at,
        is_staging=row.is_staging,
        head_sha=None,
    )


def identity_mismatch_status(row: RosterRowSpec) -> MachineStatus:
    """The MachineStatus row for a host whose ops server answered under a DIFFERENT
    machine_name than the row we targeted.

    A loud, distinct state — `online=False` + `identity_mismatch=True` — so a
    loopback/misregistered gateway_url that makes the gateway dial itself (and
    answer 200 under its own name) can no longer masquerade as the target host
    online. Structurally impossible to render as a false green.
    """
    return MachineStatus(
        name=row.name,
        serve_gateway="gateway" in row.role,
        serve_agent_runner="agent-runner" in row.role,
        serve_observability_station="observability-station" in row.role,
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

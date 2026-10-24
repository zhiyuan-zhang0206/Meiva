"""Cluster control + admin endpoints — /api/cluster/*.

Covers maintenance / status / roster / admin events query / machines
DELETE. These paths are exempt from the paused-host 503 middleware through
their CONTROL_PLANE route contracts (`base/api_contracts/contracts.py`)
because they are the tools the gateway uses during pause.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request
from psycopg_pool import ConnectionPool
from pydantic import BaseModel

from base.agents.birth.harness import LAUNCHABLE_HARNESSES
from base.api_contracts.status import MachineStatus
from base.cluster import machines
from base.db.transaction import write_transaction
from gateway.cluster.admin_events_plan import (
    AdminEventsError,
    AdminEventsFilters,
    plan_admin_events,
)
from gateway.cluster.harness_probe import HarnessProbeCache, probe_harnesses
from gateway.cluster.harness_rows import availability_rows
from gateway.cluster.ops_dispatch import dispatch_op
from gateway.cluster.roster import snapshots
from gateway.cluster.schemas import AgentMachineRow, HarnessAvailability, MachineDeleteResponse
from gateway.cluster.status import RosterEnv, gather_cluster_status, read_local_facts, roster_env
from gateway.events import telemetry_rows
from gateway.events.schemas import AgentEventRow, AgentEventsResponse
from gateway.http.config import gateway_config
from ops.cluster import operations as _ops
from ops.cluster_status import ClusterStatus

router = APIRouter()
_log = logging.getLogger(__name__)


# meiva:note
# Sync local snapshot for a pure gateway (no ops server) — via to_thread:
# the serving gate (file), the prod-source HEAD (git rev-parse) and the schema
# comparison are child-process / disk / database reads that must not run on the event loop.
def _local_snapshot_blocking(env: RosterEnv) -> ClusterStatus:
    facts = read_local_facts(env, with_resource=False)
    role = env.machine.role()
    return ClusterStatus(
        machine_name=env.machine.name(),
        serve_gateway=("gateway" in role),
        serve_agent_runner=("agent-runner" in role),
        serve_observability_station=("observability-station" in role),
        paused=facts.paused_reason is not None,
        paused_reason=facts.paused_reason,
        head_sha=facts.head_sha,
        running_sha=facts.running_sha,
        schema_mismatch=facts.schema_mismatch,
    )


async def _roster_statuses(
    env: RosterEnv, pool: ConnectionPool, *, fresh: bool
) -> list[MachineStatus]:
    """The roster of every unpaused machine: from the heartbeat liveness pass's
    snapshot, or — when `fresh` — by dialing every runner now."""
    rows = await asyncio.to_thread(snapshots.readroster_rows_blocking, pool)
    if not rows:
        return []
    found = None if fresh else await asyncio.to_thread(snapshots.read_all_blocking, pool)
    return await gather_cluster_status(env, rows, found)


@router.post("/api/cluster/stopping", status_code=200)
async def post_cluster_stopping(
    machine: str,
    home: str,
    request: Request,
    maintenance_for_seconds: Annotated[int | None, Query(gt=0, le=86_400)] = None,
    maintenance_reason: Annotated[str | None, Query(max_length=200)] = None,
) -> dict[str, str]:
    """Record that the (machine, home) unit is shutting down intentionally.

    `meiva stop` POSTs this (best-effort) just before local teardown so the
    cluster view distinguishes a deliberate stop from a crash — the live probe
    cannot. Stamps the unit's `stopped_at` and recomputes the composed
    `machines` row; `meiva start` clears it. `home` is the stopping unit's
    $MEIVA_HOME so a co-located peer unit keeps its capability.

    `machine`/`home` are caller-asserted, not verified against the auth
    principal (same trust model as the other cluster control endpoints).
    Low-stakes: stopped_at is cosmetic and a spuriously-stamped live host still
    probes online=True.
    """
    return await asyncio.to_thread(
        _ops.cluster_stopping_op,
        request.app.state.db,
        machine,
        home,
        maintenance_for_seconds,
        maintenance_reason,
    )


@router.get("/api/cluster/status")
async def get_cluster_status(request: Request) -> ClusterStatus:
    """This host's own snapshot (name / role / paused).

    On an agent-runner-capable host the snapshot comes from this host's ops
    server (a status_probe op dialed at its registered localhost URL) — the
    gateway never probes sessions/pidfiles itself. A pure gateway runs no ops
    server, so it assembles a lightweight local snapshot (paused flag +
    orchestration session + prod-source HEAD; no shell/daemon probes — that is
    agent-runner data a pure gateway does not have).

    Consumed by `meiva status`'s gateway supplement. For the full multi-machine
    roster use `/api/cluster/roster`. Bypasses 503 mode so status stays visible
    during pause — observability is always online.
    """
    return await cluster_status_snapshot(roster_env(request))


async def cluster_status_snapshot(env: RosterEnv) -> ClusterStatus:
    if "agent-runner" in env.machine.role():
        result = await dispatch_op(env.db, env.machine.name(), "status_probe", {})
        return ClusterStatus.model_validate(result)
    return await asyncio.to_thread(_local_snapshot_blocking, env)


@router.get("/api/cluster/roster", response_model=list[MachineStatus])
async def get_cluster_roster(
    request: Request,
    *,
    fresh: Annotated[
        bool, Query(description="Dial every runner now instead of reading the last probe.")
    ] = False,
) -> list[MachineStatus]:
    """The full multi-machine roster — every registered machine + status.

    Backs the thin-client `meiva cluster status`: the gateway's own row is
    resolved locally; each agent-runner renders from the heartbeat liveness
    pass's last status_probe (`MachineStatus.observed_at` says how old), so the
    read dials nothing and a down host cannot slow it. `fresh=true` probes every
    runner in parallel via the status_probe op instead (total wall ≈ the probe
    timeout regardless of N) — for a caller that must see a restart land, such as
    the fleet update. Same fan-out the `/api/status` cluster panel uses. Bypasses
    503 mode so the roster stays visible during pause.
    """
    return await _roster_statuses(
        roster_env(request), request.app.state.control_db_pool, fresh=fresh
    )


# --- Admin ops (token-only ops, ssh-free) -------------------------------------
#   - Reading service logs: the unified event stream (`telemetry_events`)
#     holds every INFO+ line from gateway / scheduler / labeler / agent-host /
#     watchdog / memory-indexer and the agent processes.
#   - Cleaning up a stale machines row after a host is decommissioned or
#     renamed: a one-shot DELETE endpoint.
# Both endpoints bypass the paused-host 503 (so they keep working during an `meiva
# update` window). The gateway is unauthenticated (the private network is the boundary).


@router.get("/api/cluster/admin/events", response_model=AgentEventsResponse)
def get_cluster_admin_events(
    request: Request,
    agent_id: int | None = None,
    service_only: bool = False,  # noqa: FBT001, FBT002 — FastAPI query param, always passed by name
    level: str | None = None,
    since: str | None = None,
    event: str | None = None,
    grep: str | None = None,
    limit: int | None = None,
) -> AgentEventsResponse:
    """Slice the unified event stream (category=telemetry/log, read from
    `telemetry_events`) for ops debugging without SSH.

    Filters compose (AND):
      - `agent_id=N`: only this agent's events (gateway / daemon rows excluded).
      - `service_only=true`: only events with no agent_id (gateway / daemons).
      - `level=ERROR`: minimum level (DEBUG/INFO/WARNING/ERROR/CRITICAL).
      - `since=2h` / `since=2026-05-25T00:00Z`: relative window or absolute
        timestamp. Relative format `<int><unit>` with unit `s/m/h/d`.
      - `event=spawn,terminate`: comma-separated event names.
      - `grep=<substring>`: case-insensitive substring match on the event
        name, source and payload (which includes the `msg` text).
      - `limit`: max rows to return, capped at 1000 (protective constant).
        Omitted returns the configured default
        (``display.cluster_events_default_limit`` - 200 out of the box).

    Returns newest-first; the client paginates by passing
    `since=<oldest_ts_seen>` on the next call.
    """
    try:
        query = plan_admin_events(
            AdminEventsFilters(
                agent_id=agent_id,
                service_only=service_only,
                level=level,
                since=since,
                event=event,
                grep=grep,
                limit=limit,
            ),
            default_limit=gateway_config(request).display.cluster_events_default_limit,
            now=datetime.now(UTC),
        )
    except AdminEventsError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    with request.app.state.db_pool.connection() as conn:
        rows, _ = telemetry_rows.query_events(
            conn,
            agent_id=query.agent_id,
            service_only=query.service_only,
            categories=list(query.categories),
            event_names=None if query.event_names is None else list(query.event_names),
            level_min=query.level_min,
            grep=query.grep,
            from_=query.from_,
            limit=query.limit,
        )
    return AgentEventsResponse(
        items=[
            AgentEventRow(
                id=row["id"],
                line_sha256=row["line_sha256"],
                ts=row["ts"],
                agent_id=row["agent_id"],
                level=row["level"],
                event=row["event_name"],
                payload=row["attributes"],
            )
            for row in rows
        ]
    )


@router.get("/api/cluster/machines", response_model=list[AgentMachineRow])
async def get_cluster_machines(
    request: Request,
    *,
    fresh: Annotated[
        bool, Query(description="Dial every runner now instead of reading the last probe.")
    ] = False,
) -> list[AgentMachineRow]:
    """List every registered machine with its description + live status.

    Backs meiva.agents.list_machines(). Live status comes from the same source the
    roster uses: the heartbeat liveness pass's last status_probe, or with
    `fresh=true` a probe of every runner now (gateway is live from its own
    perspective). role / gateway_url are intentionally omitted — agents reason
    over the free-text description, not ops topology.
    """
    statuses = await _roster_statuses(
        roster_env(request), request.app.state.control_db_pool, fresh=fresh
    )
    # This is the AGENT view: it lists only machines that can run agent processes
    # (carry the agent-runner capability). A gateway-only node is intentionally
    # invisible here; a single-box gateway,agent-runner node shows up because it
    # carries agent-runner. The operator view that shows every node is
    # `/api/status`.
    return [
        AgentMachineRow(
            name=m.name,
            description=m.description,
            # Reached-but-unknown is diagnostic visibility, not a determinate
            # liveness verdict for the SDK/config projection.
            live=m.online and m.paused is not None,
            is_staging=m.is_staging,
        )
        for m in statuses
        if m.serve_agent_runner
    ]


# meiva:note
# The spawn form reads this before offering a harness: `status` says whether that
# machine has the CLI installed and signed in, `launchable` whether this build can
# start a session on it at all. Both are reported, so an installed-but-unstartable
# harness still appears — with the reason — instead of vanishing. A machine whose
# runner predates this op, or a name no machine carries, is an error here, and the
# form then shows the choice as unknown rather than as available.
# meiva:link base/agents/docs/harness/harness.meiva.okf.md
@router.get(
    "/api/cluster/machines/{name}/harnesses",
    response_model=list[HarnessAvailability],
    description="What one machine can run, one row per harness, cached 30 seconds.",
)
async def get_machine_harnesses(name: str, request: Request) -> list[HarnessAvailability]:
    cache: HarnessProbeCache = request.app.state.harness_cache
    cached = cache.get(name)
    if cached is not None:
        return cached
    probed = await probe_harnesses(
        name, db=request.app.state.db, local_machine=request.app.state.machine
    )
    rows = availability_rows(probed, LAUNCHABLE_HARNESSES)
    cache.store(name, rows)
    return rows


class MachineStagingRequest(BaseModel):
    """Body for POST /api/cluster/machines/{name}/staging."""

    is_staging: bool


@router.post("/api/cluster/machines/{name}/staging", response_model=MachineDeleteResponse)
def set_machine_staging(
    name: str, req: MachineStagingRequest, request: Request
) -> MachineDeleteResponse:
    """Set or clear a machine's operator staging flag (`is_staging`).

    The staging latch is what keeps a registered staging host out of the
    agent-runner target set — `meiva start` on it clears its `stopped_at` like any
    host, and this flag is the exclusion (`base.cluster.machines.list_agent_runners`
    skips is_staging rows). Backed by `base.cluster.machines.set_staging`; the CLI
    verbs `meiva cluster mark-staging` / `unmark-staging` call this endpoint.
    """
    changed = machines.set_staging(request.app.state.db, name, is_staging=req.is_staging)
    if not changed:
        raise HTTPException(status_code=404, detail=f"no machine named {name!r}")
    return MachineDeleteResponse(deleted=True)


@router.delete("/api/cluster/machines/{name}", response_model=MachineDeleteResponse)
def delete_cluster_machine(name: str, request: Request) -> MachineDeleteResponse:
    """Remove a row from the `machines` table.

    Used to retire a decommissioned agent-runner or clean up after a rename
    (e.g. `laminar`→`cloud`: the new name's `register_self()` INSERTs the
    new row at startup, then ops calls this endpoint to drop the now-stale
    old-name row). Idempotent — calling on a missing name returns
    `deleted=false`.

    Refuses to delete the row corresponding to this gateway's own
    `machine_name()` — the live process needs its registration to remain
    intact (cluster status probe targets it; `register_self` only writes
    on startup, so a runtime DELETE wouldn't be repaired until next
    restart).
    """
    if name == request.app.state.machine.name():
        raise HTTPException(
            status_code=400,
            detail=f"refusing to delete this host's own machines row ({name!r}); "
            "stop the gateway first if you really want to retire it.",
        )
    with write_transaction(request.app.state.control_db_pool) as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM machines WHERE name = %s", (name,))
        deleted = cur.rowcount > 0
    return MachineDeleteResponse(deleted=deleted)

"""Fresh database projections for the agent inspector endpoint."""

from __future__ import annotations

from datetime import datetime
from typing import Any, NamedTuple

from fastapi import HTTPException
from psycopg_pool import ConnectionPool

from base.agents.observation.evidence import AgentObservation, observation
from base.agents.observation.snapshot import OpenNotice
from base.api_contracts.agent_contract import AgentStatus, Harness, LivenessState
from gateway.inspect._heartbeat import HeartbeatCadence, HeartbeatState, plan_heartbeat
from gateway.inspect.schemas import HeartbeatInfo, HeartbeatLastPause
from services.wake.heartbeat import STALE_PENDING_S


class InspectDbRows(NamedTuple):
    """Live agents_meta fields that must never ride the aggregate TTL."""

    # True only for a FRESH pending inbound (created within STALE_PENDING_S):
    # a stale one no longer counts as "about to wake" — the daemon checks in on
    # it anyway, so the panel projects next_at instead of heartbeat_pending.

    machine: str
    status: AgentStatus
    last_active_at: datetime
    last_heartbeat_at: datetime | None
    spawned_at: datetime
    paused_until: datetime | None
    pending_inbound: bool
    config_overlay: dict[str, Any]
    liveness_state: LivenessState
    last_probe_at: datetime | None
    observation: AgentObservation


def db_rows_blocking(pool: ConnectionPool[Any], agent_id: int) -> InspectDbRows:
    """Read agents_meta and the fresh pending-inbound flag in one DB borrow."""
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT config_overlay, machine, status, last_active_at, last_heartbeat_at, "
            "       spawned_at, "
            "       CASE WHEN heartbeat_paused_until > now() THEN heartbeat_paused_until END, "
            "       liveness_state, last_probe_at, "
            "       (SELECT mp.last_probe_at FROM machine_probe mp "
            "        WHERE mp.machine_name = agents_meta.machine), "
            "       harness, "
            "       (SELECT amo.model FROM agent_metric_observations amo "
            "        WHERE amo.agent_id = agents_meta.id AND amo.model IS NOT NULL "
            "        ORDER BY amo.occurred_at DESC LIMIT 1) "
            "FROM agents_meta WHERE id = %s",
            (agent_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail=f"agent {agent_id} not found")
        cur.execute(
            "SELECT EXISTS (SELECT 1 FROM inbound_messages "
            "WHERE agent_id = %s AND status = 'pending' "
            "AND created_at >= now() - make_interval(secs => %s))",
            (agent_id, STALE_PENDING_S),
        )
        pending_row = cur.fetchone()
        assert pending_row is not None  # noqa: S101 — EXISTS always returns one row
        overlay: dict[str, Any] = dict(row[0]) if row[0] is not None else {}
        if len(row) > 11 and row[10] != Harness.MEIVA and isinstance(row[11], str) and row[11]:
            overlay["llm_model"] = row[11]
        return InspectDbRows(
            machine=row[1],
            status=AgentStatus(row[2]),
            last_active_at=row[3],
            last_heartbeat_at=row[4],
            spawned_at=row[5],
            paused_until=row[6],
            pending_inbound=bool(pending_row[0]),
            config_overlay=overlay,
            liveness_state=LivenessState(row[7]),
            last_probe_at=row[8],
            observation=observation(row[9]),
        )


def notice_blocking(pool: ConnectionPool[Any], agent_id: int) -> OpenNotice | None:
    """Read the agent's single unexpired open notice, if one exists.

    The full row, including `task_id` — the inspector widget resolver
    (`gateway/inspect/_plugin_widgets.py`) reads the same notice to decide
    whether the agent's task button has a target."""
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, title, content, priority, require_response, blocking, created_at, expire_at, "
            "task_id, question "
            "FROM agent_notices "
            "WHERE agent_id = %s AND resolved_at IS NULL AND expire_at > now() "
            "AND require_response "
            "ORDER BY created_at DESC LIMIT 1",
            (agent_id,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return OpenNotice(
        id=row[0],
        title=row[1],
        content=row[2],
        priority=row[3],
        require_response=row[4],
        blocking=row[5],
        created_at=row[6],
        expire_at=row[7],
        task_id=row[8],
        question=row[9],
    )


# meiva:note
# The inspector's heartbeat block: the daemon's projection (`plan_heartbeat`) plus the
# configured interval and the latest recent committed row from heartbeat_pause_log.
#
# Every input comes from the request's fresh agents_meta projection; current heartbeat state
# does not depend on historical statistics or logs.
def project_heartbeat(
    state: HeartbeatState, cadence: HeartbeatCadence, last_pause: HeartbeatLastPause | None
) -> HeartbeatInfo:
    plan = plan_heartbeat(state, cadence)
    return HeartbeatInfo(
        interval_s=cadence.interval_s,
        next_at=plan.next_at,
        paused_until=plan.paused_until,
        heartbeat_pending=plan.heartbeat_pending,
        last_pause=last_pause,
    )

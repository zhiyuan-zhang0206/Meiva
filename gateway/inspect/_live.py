"""Fresh database projections for the agent inspector endpoint."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import NamedTuple

from fastapi import HTTPException
from psycopg import sql as pgsql
from psycopg_pool import ConnectionPool

from base.agents.messages.inbound import not_passive_inbound
from base.agents.observation.evidence import AgentObservation, observation
from base.agents.observation.snapshot import OpenNotice
from base.api_contracts.agent_contract import AgentStatus, Harness, LivenessState
from base.db.rows import query_one
from base.host.boundary.json_values import is_object_dict
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
    config_overlay: dict[str, object]
    liveness_state: LivenessState
    last_probe_at: datetime | None
    observation: AgentObservation
    backoff_level: int
    impersonation_since: datetime | None


# meiva:note
# One `agents_meta` row with the live facts the inspector needs; `config_overlay` is the stored
# jsonb and `harness` NULL for a row born before the column existed.
@dataclass(frozen=True)
class _AgentLiveRow:
    config_overlay: object
    machine: str
    status: str
    last_active_at: datetime
    last_heartbeat_at: datetime | None
    spawned_at: datetime
    paused_until: datetime | None
    liveness_state: str
    last_probe_at: datetime | None
    machine_probed_at: datetime | None
    harness: str | None
    observed_model: str | None
    heartbeat_backoff_level: int | None
    impersonation_since: datetime | None


# meiva:note
# Whether a fresh pending inbound exists.
@dataclass(frozen=True)
class _PendingRow:
    pending: bool


def db_rows_blocking(pool: ConnectionPool, agent_id: int) -> InspectDbRows:
    """Read agents_meta and the fresh pending-inbound flag in one DB borrow."""
    with pool.connection() as conn:
        row = query_one(
            conn,
            _AgentLiveRow,
            "SELECT config_overlay, machine, status, last_active_at, last_heartbeat_at, "
            "       spawned_at, "
            "       CASE WHEN heartbeat_paused_until > now() THEN heartbeat_paused_until END "
            "         AS paused_until, "
            "       liveness_state, last_probe_at, "
            "       (SELECT mp.last_probe_at FROM machine_probe mp "
            "        WHERE mp.machine_name = agents_meta.machine) AS machine_probed_at, "
            "       harness, "
            "       (SELECT amo.model FROM agent_metric_observations amo "
            "        WHERE amo.agent_id = agents_meta.id AND amo.model IS NOT NULL "
            "        AND amo.occurred_at >= COALESCE(agents_meta.harness_configured_at, '-infinity') "
            "        ORDER BY amo.occurred_at DESC LIMIT 1) AS observed_model, "
            "       heartbeat_backoff_level, "
            "       (SELECT COALESCE(lease.activated_at, lease.created_at) "
            "        FROM agent_impersonations lease "
            "        WHERE lease.agent_id = agents_meta.id AND lease.status = 'active' "
            "        AND lease.expires_at > now()) AS impersonation_since "
            "FROM agents_meta WHERE id = %s",
            (agent_id,),
        )
        if row is None:
            raise HTTPException(status_code=404, detail=f"agent {agent_id} not found")
        pending_row = query_one(
            conn,
            _PendingRow,
            pgsql.SQL(
                "SELECT EXISTS (SELECT 1 FROM inbound_messages "
                "WHERE agent_id = %s AND status = 'pending' "
                "AND {not_passive} "
                "AND created_at >= now() - make_interval(secs => %s)) AS pending"
            ).format(not_passive=pgsql.SQL(not_passive_inbound())),
            (agent_id, STALE_PENDING_S),
        )
        assert pending_row is not None  # noqa: S101 — EXISTS always returns one row
        overlay: dict[str, object] = (
            dict(row.config_overlay) if is_object_dict(row.config_overlay) else {}
        )
        if row.harness != Harness.MEIVA and row.observed_model:
            overlay["llm_model"] = row.observed_model
        return InspectDbRows(
            machine=row.machine,
            status=AgentStatus(row.status),
            last_active_at=row.last_active_at,
            last_heartbeat_at=row.last_heartbeat_at,
            spawned_at=row.spawned_at,
            paused_until=row.paused_until,
            pending_inbound=pending_row.pending,
            config_overlay=overlay,
            liveness_state=LivenessState(row.liveness_state),
            last_probe_at=row.last_probe_at,
            observation=observation(row.machine_probed_at),
            backoff_level=int(row.heartbeat_backoff_level or 0),
            impersonation_since=row.impersonation_since,
        )


def notice_blocking(pool: ConnectionPool, agent_id: int) -> OpenNotice | None:
    """Read the agent's single unexpired open notice, if one exists.

    The full row, including `task_id` — the inspector widget resolver
    (`gateway/inspect/_plugin_widgets.py`) reads the same notice to decide
    whether the agent's task button has a target."""
    with pool.connection() as conn:
        return query_one(
            conn,
            OpenNotice,
            "SELECT id, title, content, priority, require_response, blocking, created_at, expire_at, "
            "task_id, question "
            "FROM agent_notices "
            "WHERE agent_id = %s AND resolved_at IS NULL AND (expire_at IS NULL OR expire_at > now()) "
            "AND require_response "
            "ORDER BY created_at DESC LIMIT 1",
            (agent_id,),
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

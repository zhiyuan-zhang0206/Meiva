# meiva:note
# Shared schedule read projections for HTTP mutations and queries.

from __future__ import annotations

import psycopg
from fastapi import HTTPException
from psycopg import sql
from psycopg_pool import ConnectionPool

from base.db.rows import query_all, query_one
from gateway.schedules.contracts import ScheduleSummary, ScheduleView
from gateway.schedules.placement import MachineSnapshot, judge_placement
from gateway.schedules.rows import ScheduleFullRow, ScheduleSummaryRow

SCHEDULE_BASE_COLS = (
    "s.id, s.name, s.description, s.command, s.enabled, s.status, s.last_error, "
    "s.created_at, s.updated_at, s.machine, s.owner_plugin"
)
OWNER_COL = "s.owner_agent_id"
TRIGGER_COLS = (
    "s.trigger_kind, s.run_at, s.cron_expr, s.interval_seconds, s.timezone, "
    "s.timeout_seconds, s.on_owner_terminate, s.next_launch_at"
)
PLACEMENT_COLS = (
    "m.name IS NOT NULL AS machine_registered, m.paused_at, m.pause_reason, "
    "m.stopped_at, m.maintenance_reason, mp.online, mp.consecutive_failures"
)
JOIN_CLAUSE = (
    "FROM schedules s "
    "LEFT JOIN machines m ON m.name = s.machine "
    "LEFT JOIN machine_probe mp ON mp.machine_name = s.machine"
)
SUMMARY_QUERY = (
    f"SELECT {SCHEDULE_BASE_COLS}, {PLACEMENT_COLS}, {OWNER_COL}, {TRIGGER_COLS} "
    f"{JOIN_CLAUSE} ORDER BY s.id DESC"
)
# meiva:note
# Trigger columns stay last. The operator repair script indexes ScheduleFullRow positionally
# across installed gateway versions, so inserting a column before script would shift old fields.
FULL_QUERY = (
    f"SELECT {SCHEDULE_BASE_COLS}, s.script, {PLACEMENT_COLS}, {OWNER_COL}, {TRIGGER_COLS} "
    f"{JOIN_CLAUSE} WHERE s.id = %s"
)
MACHINE_PLACEMENT_QUERY = sql.SQL(
    "SELECT {} FROM (SELECT %s::text AS target) t "
    "LEFT JOIN machines m ON m.name = t.target "
    "LEFT JOIN machine_probe mp ON mp.machine_name = t.target"
).format(sql.SQL(PLACEMENT_COLS))
SCHEDULE_STORE_COLS = (
    "id, name, description, command, enabled, status, last_error, created_at, updated_at, "
    "machine, owner_plugin, script, owner_agent_id, trigger_kind, run_at, cron_expr, "
    "interval_seconds, timezone, timeout_seconds, on_owner_terminate, next_launch_at"
)


def _snapshot(row: ScheduleSummaryRow | ScheduleFullRow) -> MachineSnapshot:
    return MachineSnapshot(
        registered=row.machine_registered,
        paused_at=row.paused_at,
        pause_reason=row.pause_reason,
        stopped_at=row.stopped_at,
        maintenance_reason=row.maintenance_reason,
        online=row.online,
        consecutive_failures=row.consecutive_failures or 0,
    )


def summary(row: ScheduleSummaryRow | ScheduleFullRow) -> ScheduleSummary:
    placement = judge_placement(machine=row.machine, snapshot=_snapshot(row))
    return ScheduleSummary(
        id=row.id,
        name=row.name,
        description=row.description,
        command=row.command,
        enabled=row.enabled,
        status=row.status,
        last_error=row.last_error,
        created_at=row.created_at,
        updated_at=row.updated_at,
        machine=row.machine,
        owner_plugin=row.owner_plugin,
        owner_agent_id=row.owner_agent_id,
        trigger_kind=row.trigger_kind,
        run_at=row.run_at,
        cron_expr=row.cron_expr,
        interval_seconds=row.interval_seconds,
        timezone=row.timezone,
        timeout_seconds=row.timeout_seconds,
        on_owner_terminate=row.on_owner_terminate,
        next_launch_at=row.next_launch_at,
        placement_status=placement.status,
        placement_reason=placement.reason,
    )


def view(row: ScheduleFullRow) -> ScheduleView:
    projected = summary(row)
    return ScheduleView(
        id=projected.id,
        name=projected.name,
        description=projected.description,
        command=projected.command,
        enabled=projected.enabled,
        status=projected.status,
        last_error=projected.last_error,
        created_at=projected.created_at,
        updated_at=projected.updated_at,
        machine=projected.machine,
        owner_plugin=projected.owner_plugin,
        owner_agent_id=projected.owner_agent_id,
        trigger_kind=projected.trigger_kind,
        run_at=projected.run_at,
        cron_expr=projected.cron_expr,
        interval_seconds=projected.interval_seconds,
        timezone=projected.timezone,
        timeout_seconds=projected.timeout_seconds,
        on_owner_terminate=projected.on_owner_terminate,
        next_launch_at=projected.next_launch_at,
        placement_status=projected.placement_status,
        placement_reason=projected.placement_reason,
        script=row.script,
    )


def list_blocking(pool: ConnectionPool) -> list[ScheduleSummary]:
    with pool.connection() as conn:
        return [summary(row) for row in query_all(conn, ScheduleSummaryRow, SUMMARY_QUERY)]


def fetch_full_blocking(pool: ConnectionPool, schedule_id: int) -> ScheduleFullRow:
    with pool.connection() as conn:
        row = query_one(conn, ScheduleFullRow, FULL_QUERY, (schedule_id,))
    if row is None:
        raise HTTPException(status_code=404, detail=f"schedule {schedule_id} not found")
    return row


def refuse_active_relocation(conn: psycopg.Connection, schedule_id: int) -> None:
    open_run = conn.execute(
        "SELECT 1 FROM schedule_runs WHERE schedule_id = %s AND ok IS NULL", (schedule_id,)
    ).fetchone()
    if open_run is not None:
        raise HTTPException(status_code=409, detail="schedule is running; wait for it to finish")

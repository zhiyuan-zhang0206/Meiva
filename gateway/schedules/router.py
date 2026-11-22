from __future__ import annotations

import asyncio
import shutil
from datetime import UTC, datetime
from pathlib import Path

import psycopg
from fastapi import APIRouter, HTTPException, Request
from psycopg import sql
from psycopg.rows import class_row
from psycopg_pool import ConnectionPool

from base.db.rows import query_all, query_one
from base.db.transaction import write_transaction
from base.telemetry.alerts import resolve_schedule_alerts
from gateway.http.config import (
    gateway_clock,
    gateway_config,
    gateway_rpc,
    gateway_state,
)
from gateway.routers.receipts import creation as creation_receipts
from gateway.schedules import plans, receipts, triggers
from gateway.schedules.auxiliary_routes import router as auxiliary_router
from gateway.schedules.contracts import (
    DEFAULT_COMMAND,
    ScheduleCreate,
    ScheduleLogsView,
    ScheduleRunView,
    ScheduleSummary,
    ScheduleUpdate,
    ScheduleView,
)
from gateway.schedules.repository import (
    FULL_QUERY as _FULL_QUERY,
)
from gateway.schedules.repository import (
    MACHINE_PLACEMENT_QUERY as _MACHINE_PLACEMENT_QUERY,
)
from gateway.schedules.repository import (
    SCHEDULE_STORE_COLS as _SCHEDULE_STORE_COLS,
)
from gateway.schedules.repository import (
    fetch_full_blocking,
    refuse_active_relocation,
)
from gateway.schedules.repository import (
    list_blocking as _list_blocking,
)
from gateway.schedules.repository import (
    view as _view,
)
from gateway.schedules.rows import (
    ControlRow,
    DeletedRow,
    InsertedId,
    OwnerMachineRow,
    OwnerRow,
    PlacementRow,
    ScheduleFullRow,
    ScheduleStoreRow,
)
from ops.cluster import rpc as cluster_rpc
from ops.rpc_schemas import ScheduleCaptureResult

router = APIRouter()
router.include_router(auxiliary_router)

_CAPTURE_TIMEOUT_S = 10.0

_DEFAULT_COMMAND = DEFAULT_COMMAND


def _validate_script(script: str) -> None:
    try:
        compile(script, "<schedule>", "exec")
    except SyntaxError as exc:
        raise HTTPException(
            status_code=400, detail=f"script has a syntax error (line {exc.lineno}): {exc.msg}"
        ) from exc


def _edit_assignments(changed: dict[str, object]) -> list[sql.Composable]:
    assignments: list[sql.Composable] = [
        sql.SQL("{} = %s").format(sql.Identifier(col)) for col in changed
    ]
    assignments.append(sql.SQL("updated_at = now()"))
    return assignments


def _refuse_plugin_mutation(owner_plugin: str | None, changed: dict[str, object]) -> None:
    if owner_plugin is not None and (set(changed) - {"enabled"}):
        raise HTTPException(
            status_code=409,
            detail=(
                f"schedule is owned by plugin '{owner_plugin}'; "
                "only enabled may be toggled directly"
            ),
        )


def _apply_schedule_update(
    cur: psycopg.Cursor,
    schedule_id: int,
    previous: ScheduleStoreRow,
    changed: dict[str, object],
    fields: dict[str, object],
    now: datetime,
) -> None:
    code_changed = "script" in changed or "command" in changed
    if "enabled" in changed:
        enabled = bool(changed["enabled"])
        changed["status"] = "scheduled" if enabled else "stopped"
        changed["next_launch_at"] = (
            triggers.enabled_next_launch(
                ControlRow(
                    enabled=previous.enabled,
                    status=previous.status,
                    machine=previous.machine,
                    trigger_kind=previous.trigger_kind,
                    run_at=previous.run_at,
                    cron_expr=previous.cron_expr,
                    interval_seconds=previous.interval_seconds,
                    timezone=previous.timezone,
                ),
                now,
            )
            if enabled
            else None
        )
    assignments = _edit_assignments(changed)
    cur.execute(
        sql.SQL("UPDATE schedules SET {} WHERE id = %s").format(sql.SQL(", ").join(assignments)),
        (*changed.values(), schedule_id),
    )
    effective_enabled = changed.get("enabled", previous.enabled)
    effective_status = changed.get("status", previous.status)
    if not effective_enabled or effective_status in ("stopped", "completed", "cancelled"):
        resolve_schedule_alerts(cur.connection, schedule_id)
    if code_changed:
        cur.execute(
            "INSERT INTO schedule_versions (schedule_id, script, command, note) "
            "VALUES (%s, %s, %s, %s)",
            (
                schedule_id,
                fields.get("script", previous.script),
                fields.get("command", previous.command),
                "edit",
            ),
        )


# meiva:link gateway/schedules/docs/router-contracts.md
def update_blocking(
    pool: ConnectionPool,
    schedule_id: int,
    fields: dict[str, object],
    key: str | None,
    now: datetime,
) -> tuple[ScheduleFullRow, bool]:

    if "script" in fields:
        script = fields["script"]
        if not isinstance(script, str):
            raise TypeError("schedule script must be a string")
        _validate_script(script)

    try:
        with write_transaction(pool) as conn, conn.cursor() as cur:
            replay = receipts.begin(conn, key, {"schedule_id": schedule_id, "fields": fields})
            if replay is not None:
                return replay, False
            previous = query_one(
                conn,
                ScheduleStoreRow,
                sql.SQL("SELECT {} FROM schedules WHERE id = %s FOR UPDATE").format(
                    sql.SQL(_SCHEDULE_STORE_COLS)
                ),
                (schedule_id,),
            )
            if previous is None:
                raise HTTPException(status_code=404, detail=f"schedule {schedule_id} not found")
            changed = plans.changed_fields(previous, fields)
            _refuse_plugin_mutation(previous.owner_plugin, changed)
            if "machine" in changed:
                refuse_active_relocation(conn, schedule_id)
            if not changed:
                full_row = query_one(conn, ScheduleFullRow, _FULL_QUERY, (schedule_id,))
                if full_row is None:
                    raise HTTPException(status_code=404, detail=f"schedule {schedule_id} not found")
                receipts.finish(conn, key, full_row)
                return full_row, False
            _apply_schedule_update(cur, schedule_id, previous, changed, fields, now)
            needs_sync = False
            row = query_one(conn, ScheduleFullRow, _FULL_QUERY, (schedule_id,))
            if row is None:
                raise HTTPException(status_code=404, detail=f"schedule {schedule_id} not found")
            receipts.finish(conn, key, row)
    except psycopg.errors.UniqueViolation as exc:
        raise HTTPException(
            status_code=409, detail=f"schedule named {fields['name']!r} already exists"
        ) from exc
    except psycopg.errors.ForeignKeyViolation as exc:
        raise HTTPException(status_code=400, detail="owner_agent_id does not exist") from exc
    return row, needs_sync


# meiva:note
# The interpolated column list is a module constant; the values it filters on stay parameterized.
@router.get(
    "/api/schedules",
    description="List all schedules (enabled and disabled), newest first. Omits `script`.",
)
async def list_schedules(request: Request) -> list[ScheduleSummary]:
    return await asyncio.to_thread(_list_blocking, gateway_state(request).db_pool)


def _reconstruct_created_row(
    conn: psycopg.Connection,
    created: creation_receipts.Creation,
    body: ScheduleCreate,
    machine: str,
    cluster_timezone: str,
) -> ScheduleFullRow:
    prepared = triggers.prepare(body, cluster_timezone=cluster_timezone, now=created.created_at)
    placement = query_one(conn, PlacementRow, _MACHINE_PLACEMENT_QUERY, (machine,))
    if placement is None:
        raise RuntimeError("LEFT JOIN against scalar target returned no row")
    return ScheduleFullRow(
        id=created.resource_id,
        name=body.name,
        description=body.description,
        command=body.command,
        enabled=body.enabled,
        status="scheduled" if body.enabled else "stopped",
        last_error=None,
        created_at=created.created_at,
        updated_at=created.updated_at,
        machine=machine,
        owner_plugin=None,
        script=prepared.script,
        machine_registered=placement.machine_registered,
        paused_at=placement.paused_at,
        pause_reason=placement.pause_reason,
        stopped_at=placement.stopped_at,
        maintenance_reason=placement.maintenance_reason,
        online=placement.online,
        consecutive_failures=placement.consecutive_failures,
        owner_agent_id=body.owner_agent_id,
        trigger_kind=body.trigger_kind,
        run_at=body.run_at,
        cron_expr=body.cron_expr,
        interval_seconds=body.interval_seconds,
        timezone=prepared.timezone,
        timeout_seconds=body.timeout_seconds,
        on_owner_terminate=prepared.on_owner_terminate,
        next_launch_at=prepared.next_launch_at,
        created_by_agent_id=body.created_by_agent_id,
    )


# meiva:link gateway/schedules/docs/router-contracts.md
def create_blocking(
    pool: ConnectionPool,
    body: ScheduleCreate,
    machine: str,
    cluster_timezone: str,
    now: datetime,
    key: str | None = None,
) -> ScheduleFullRow:
    now = now.astimezone(UTC)
    prepared = triggers.prepare(body, cluster_timezone=cluster_timezone, now=now)
    _validate_script(prepared.script)
    try:
        with (
            write_transaction(pool) as conn,
            conn.cursor(row_factory=class_row(InsertedId)) as cur,
        ):
            created = creation_receipts.begin(
                conn,
                key,
                {"resource": "schedule", "body": body.model_dump(mode="json"), "machine": machine},
            )
            if created is not None:
                return _reconstruct_created_row(conn, created, body, machine, cluster_timezone)
            cur.execute(
                "INSERT INTO schedules "
                "(name, description, script, command, enabled, status, machine, owner_agent_id, "
                "created_by_agent_id, trigger_kind, run_at, cron_expr, interval_seconds, "
                "timezone, timeout_seconds, on_owner_terminate, next_launch_at, created_at, "
                "updated_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
                "%s, %s) "
                "RETURNING id",
                (
                    body.name,
                    body.description,
                    prepared.script,
                    body.command,
                    body.enabled,
                    "scheduled" if body.enabled else "stopped",
                    machine,
                    body.owner_agent_id,
                    body.created_by_agent_id,
                    body.trigger_kind,
                    body.run_at,
                    body.cron_expr,
                    body.interval_seconds,
                    prepared.timezone,
                    body.timeout_seconds,
                    prepared.on_owner_terminate,
                    prepared.next_launch_at,
                    now,
                    now,
                ),
            )
            inserted = cur.fetchone()
            assert inserted is not None  # noqa: S101
            schedule_id = inserted.id
            cur.execute(
                "INSERT INTO schedule_versions (schedule_id, script, command, note) "
                "VALUES (%s, %s, %s, %s)",
                (schedule_id, prepared.script, body.command, "initial"),
            )
            row = query_one(conn, ScheduleFullRow, _FULL_QUERY, (schedule_id,))
            assert row is not None  # noqa: S101
            creation_receipts.finish(
                conn, key, creation_receipts.Creation(row.id, row.created_at, row.updated_at)
            )
    except psycopg.errors.UniqueViolation as exc:
        raise HTTPException(
            status_code=409, detail=f"schedule named {body.name!r} already exists"
        ) from exc
    except psycopg.errors.ForeignKeyViolation as exc:
        constraint = exc.diag.constraint_name or ""
        detail = (
            "created_by_agent_id does not exist"
            if "created_by_agent_id" in constraint
            else "owner_agent_id does not exist"
        )
        raise HTTPException(status_code=400, detail=detail) from exc
    return row


def _owner_machine_blocking(pool: ConnectionPool, owner_agent_id: int) -> str:
    with pool.connection() as conn:
        row = query_one(
            conn,
            OwnerMachineRow,
            "SELECT machine FROM agents_meta WHERE id = %s",
            (owner_agent_id,),
        )
    if row is None:
        raise HTTPException(status_code=400, detail=f"owner agent {owner_agent_id} not found")
    machine = row.machine
    if not machine:
        raise HTTPException(status_code=409, detail=f"owner agent {owner_agent_id} has no machine")
    return machine


@router.post(
    "/api/schedules",
    status_code=201,
    description="Create a schedule. 400 on a script syntax error, 409 on a name clash.",
)
async def create_schedule(request: Request, body: ScheduleCreate) -> ScheduleView:
    if body.machine is not None:
        machine = body.machine.strip()
    elif body.owner_agent_id is not None:
        machine = await asyncio.to_thread(
            _owner_machine_blocking, gateway_state(request).db_pool, body.owner_agent_id
        )
    else:
        machine = gateway_state(request).machine.name()
    if not machine:
        raise HTTPException(status_code=400, detail="machine cannot be empty")
    row = await asyncio.to_thread(
        create_blocking,
        gateway_state(request).db_pool,
        body,
        machine,
        gateway_config(request).clock.timezone,
        gateway_clock(request).now(),
        creation_receipts.operation_key(request),
    )
    return _view(row)


@router.get(
    "/api/schedules/{schedule_id}",
    description="Get one schedule (with its script). 404 if it does not exist.",
)
async def get_schedule(request: Request, schedule_id: int) -> ScheduleView:
    row = await asyncio.to_thread(fetch_full_blocking, gateway_state(request).db_pool, schedule_id)
    return _view(row)


@router.put(
    "/api/schedules/{schedule_id}",
    description="Partial-update a schedule. Script/command changes are versioned. Declarative "
    "schedules apply changes on their next fire. "
    "400 on invalid input, 404 if missing, 409 on a name clash.",
)
async def update_schedule(request: Request, schedule_id: int, body: ScheduleUpdate) -> ScheduleView:
    fields: dict[str, object] = body.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(status_code=400, detail="no fields to update")
    owner_agent_id = fields.get("owner_agent_id")
    if "machine" in fields:
        machine = fields["machine"]
        if not isinstance(machine, str) or not machine.strip():
            raise HTTPException(status_code=400, detail="machine cannot be empty")
        fields["machine"] = machine.strip()
    if "machine" not in fields and isinstance(owner_agent_id, int):
        fields["machine"] = await asyncio.to_thread(
            _owner_machine_blocking,
            gateway_state(request).db_pool,
            owner_agent_id,
        )
    if isinstance(owner_agent_id, int) and "on_owner_terminate" not in fields:
        fields["on_owner_terminate"] = "cancel"
    row, _changed = await asyncio.to_thread(
        update_blocking,
        gateway_state(request).db_pool,
        schedule_id,
        fields,
        receipts.operation_key(request),
        gateway_clock(request).now(),
    )
    return _view(row)


def delete_blocking(pool: ConnectionPool, schedule_id: int) -> DeletedRow:
    with write_transaction(pool) as conn:
        row = query_one(
            conn,
            OwnerRow,
            "SELECT owner_plugin, trigger_kind FROM schedules WHERE id = %s FOR UPDATE",
            (schedule_id,),
        )
        if row is None:
            raise HTTPException(status_code=404, detail=f"schedule {schedule_id} not found")
        if row.owner_plugin is not None:
            raise HTTPException(
                status_code=409,
                detail=f"cannot delete schedule: owned by plugin '{row.owner_plugin}'",
            )
        refuse_active_relocation(conn, schedule_id)
        deleted = query_one(
            conn,
            DeletedRow,
            "DELETE FROM schedules WHERE id = %s RETURNING machine, owner_agent_id, trigger_kind",
            (schedule_id,),
        )
        if deleted is None:
            raise HTTPException(status_code=404, detail=f"schedule {schedule_id} not found")
        resolve_schedule_alerts(conn, schedule_id)
        return deleted


def _remove_schedule_dir(home: Path, owner_agent_id: int | None, schedule_id: int) -> None:
    if owner_agent_id is None:
        root = home / "schedules" / str(schedule_id)
    else:
        root = home / "workspaces" / str(owner_agent_id) / "schedules" / str(schedule_id)
    shutil.rmtree(root, ignore_errors=True)


@router.delete(
    "/api/schedules/{schedule_id}",
    description="Delete a schedule and remove its local work directory. 404 if missing.",
)
async def delete_schedule(request: Request, schedule_id: int) -> dict[str, str]:
    pool = gateway_state(request).db_pool
    deleted = await asyncio.to_thread(delete_blocking, pool, schedule_id)
    if deleted.machine == gateway_state(request).machine.name():
        await asyncio.to_thread(
            _remove_schedule_dir,
            gateway_state(request).gateway_process.home,
            deleted.owner_agent_id,
            schedule_id,
        )
    return {"status": "deleted"}


@router.post(
    "/api/schedules/{schedule_id}/start",
    description="Enable the schedule and compute its next fire. 404 if missing.",
)
async def start_schedule(request: Request, schedule_id: int) -> ScheduleView:
    return await _control_and_sync(request, schedule_id, action="start")


@router.post(
    "/api/schedules/{schedule_id}/stop",
    description="Disable future fires. 404 if missing.",
)
async def stop_schedule(request: Request, schedule_id: int) -> ScheduleView:
    return await _control_and_sync(request, schedule_id, action="stop")


@router.post(
    "/api/schedules/{schedule_id}/restart",
    description="Run a schedule now. 404 if missing, 409 if disabled.",
)
async def restart_schedule(request: Request, schedule_id: int) -> ScheduleView:
    return await _control_and_sync(request, schedule_id, action="restart")


@router.post("/api/schedules/{schedule_id}/enable")
async def enable_schedule(request: Request, schedule_id: int) -> ScheduleView:
    return await _control_and_sync(request, schedule_id, action="enable")


@router.post("/api/schedules/{schedule_id}/disable")
async def disable_schedule(request: Request, schedule_id: int) -> ScheduleView:
    return await _control_and_sync(request, schedule_id, action="disable")


@router.post("/api/schedules/{schedule_id}/cancel")
async def cancel_schedule(request: Request, schedule_id: int) -> ScheduleView:
    return await _control_and_sync(request, schedule_id, action="cancel")


@router.post("/api/schedules/{schedule_id}/run-now")
async def run_schedule_now(request: Request, schedule_id: int) -> ScheduleView:
    return await _control_and_sync(request, schedule_id, action="run_now")


def _read_declarative_log(
    home: Path, schedule_id: int, owner_agent_id: int | None, lines: int
) -> list[str] | None:
    if owner_agent_id is None:
        path = home / "schedules" / str(schedule_id) / "latest.log"
    else:
        path = (
            home
            / "workspaces"
            / str(owner_agent_id)
            / "schedules"
            / str(schedule_id)
            / "latest.log"
        )
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:]
    except OSError:
        return None


@router.get(
    "/api/schedules/{schedule_id}/logs",
    description="Recent output from latest.log or last_error. 404 if missing.",
)
async def get_schedule_logs(
    request: Request, schedule_id: int, lines: int = 200
) -> ScheduleLogsView:

    row = await asyncio.to_thread(fetch_full_blocking, gateway_state(request).db_pool, schedule_id)
    target_machine = row.machine
    local_machine = gateway_state(request).machine.name()
    window = plans.capture_window(lines)

    if target_machine == local_machine:
        captured = None
        transcript = await asyncio.to_thread(
            _read_declarative_log,
            gateway_state(request).gateway_process.home,
            schedule_id,
            row.owner_agent_id,
            window,
        )
    else:
        captured = None
        transcript = None
        try:
            result = await gateway_rpc(request).dispatch_to_machine(
                gateway_state(request).db,
                target_machine,
                "schedule_capture",
                {
                    "schedule_id": schedule_id,
                    "lines": window,
                    "owner_agent_id": row.owner_agent_id,
                },
                timeout_s=_CAPTURE_TIMEOUT_S,
            )
            res_obj = ScheduleCaptureResult.model_validate(result)
            captured = res_obj.captured
            transcript = res_obj.transcript
        except cluster_rpc.ClusterOpUnreachable as exc:
            raise HTTPException(
                status_code=503,
                detail=f"machine {target_machine!r} ops server unreachable for schedule_capture: {exc!s}",
            ) from exc
        except cluster_rpc.ClusterOpFailed:
            captured = None
            transcript = None

    source, shown = plans.logs_view(
        lines, captured=captured, transcript=transcript, last_error=row.last_error
    )
    if source == "transcript":
        source = "latest"
    return ScheduleLogsView(source=source, lines=shown)


def _runs_blocking(pool: ConnectionPool, schedule_id: int, limit: int) -> list[ScheduleRunView]:

    with pool.connection() as conn:
        return query_all(
            conn,
            ScheduleRunView,
            "SELECT id, ran_at, ok, agent_id, note, scheduled_for FROM schedule_runs "
            "WHERE schedule_id = %s ORDER BY ran_at DESC, id DESC LIMIT %s",
            (schedule_id, limit),
        )


@router.get(
    "/api/schedules/{schedule_id}/runs",
    description="Recent run-history rows (newest first). Omit `limit` for the configured\ndefault window (``display.schedules_runs_default_limit`` - 50 out of the\nbox). 404 if the schedule is missing.",
)
async def get_schedule_runs(
    request: Request, schedule_id: int, limit: int | None = None
) -> list[ScheduleRunView]:

    if limit is None:
        limit = gateway_config(request).display.schedules_runs_default_limit
    pool = gateway_state(request).db_pool
    await asyncio.to_thread(fetch_full_blocking, pool, schedule_id)
    return await asyncio.to_thread(_runs_blocking, pool, schedule_id, limit)


def _apply_declarative_control(
    conn: psycopg.Connection,
    schedule_id: int,
    action: plans.ControlAction,
    row: ControlRow,
    now: datetime,
) -> None:
    if action in ("restart", "run_now"):
        if (
            conn.execute(
                "SELECT 1 FROM schedule_runs WHERE schedule_id = %s AND ok IS NULL", (schedule_id,)
            ).fetchone()
            is not None
        ):
            raise HTTPException(status_code=409, detail="schedule is already running")
        enabled, status, next_launch = True, "scheduled", now
    elif action in ("start", "enable"):
        enabled, status = True, "scheduled"
        next_launch = triggers.enabled_next_launch(row, now)
    elif action in ("stop", "disable"):
        enabled, status, next_launch = False, "stopped", None
    else:
        enabled, status, next_launch = False, "cancelled", None
    conn.execute(
        "UPDATE schedules SET enabled = %s, status = %s, next_launch_at = %s, "
        "last_error = NULL, updated_at = %s WHERE id = %s",
        (enabled, status, next_launch, now, schedule_id),
    )
    if not enabled:
        resolve_schedule_alerts(conn, schedule_id)


# meiva:link gateway/schedules/docs/router-contracts.md
def control_blocking(
    pool: ConnectionPool,
    schedule_id: int,
    action: plans.ControlAction,
    key: str | None,
    now: datetime,
) -> tuple[ScheduleFullRow, bool]:
    now = now.astimezone(UTC)
    with write_transaction(pool) as conn:
        replay = receipts.begin(conn, key, {"schedule_id": schedule_id, "action": action})
        if replay is not None:
            return replay, False
        row = query_one(
            conn,
            ControlRow,
            "SELECT enabled, status, machine, trigger_kind, run_at, cron_expr, "
            "interval_seconds, timezone FROM schedules WHERE id = %s FOR UPDATE",
            (schedule_id,),
        )
        if row is None:
            raise HTTPException(status_code=404, detail=f"schedule {schedule_id} not found")
        plan = plans.control_plan(action, enabled=row.enabled, status=row.status)
        if plan.refusal is not None:
            raise HTTPException(status_code=409, detail=plan.refusal)
        if plan.needs_sync:
            _apply_declarative_control(conn, schedule_id, action, row, now)
        full_row = query_one(conn, ScheduleFullRow, _FULL_QUERY, (schedule_id,))
        assert full_row is not None  # noqa: S101
        receipts.finish(conn, key, full_row)
    return full_row, plan.needs_sync


async def _control_and_sync(
    request: Request, schedule_id: int, *, action: plans.ControlAction
) -> ScheduleView:
    pool = gateway_state(request).db_pool
    row, _changed = await asyncio.to_thread(
        control_blocking,
        pool,
        schedule_id,
        action,
        receipts.operation_key(request),
        gateway_clock(request).now(),
    )
    return _view(row)

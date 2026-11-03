from __future__ import annotations

import asyncio
import shutil
from datetime import datetime
from pathlib import Path

import psycopg
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from psycopg import sql
from psycopg.rows import class_row
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, Field

from base.cluster import session_name
from base.db.transaction import write_transaction
from base.sessions.protocol import SessionBackend
from gateway.agents.creation import create_and_launch_agent, guarded_draft_key
from gateway.http.auth.request_principal import cluster_credential
from gateway.http.config import (
    GatewayConfig,
    gateway_clock,
    gateway_config,
    gateway_rpc,
    gateway_state,
)
from gateway.http.db_rows import query_all, query_one
from gateway.http.model_catalog import require_model_catalog
from gateway.routers.receipts import creation as creation_receipts
from gateway.schedules import plans, receipts, session_control
from gateway.schedules.placement import MachineSnapshot, PlacementStatus, judge_placement
from gateway.schedules.rows import (
    ControlRow,
    DeletedRow,
    InsertedId,
    LaunchEnvRow,
    OwnerRow,
    PlacementRow,
    ScheduleFullRow,
    ScheduleStoreRow,
    ScheduleSummaryRow,
)
from ops.cluster import rpc as cluster_rpc
from ops.rpc_schemas import ScheduleCaptureResult, SpawnAgentRequest

router = APIRouter()

_CAPTURE_TIMEOUT_S = 10.0

_SCHEDULE_BASE_COLS = (
    "s.id, s.name, s.description, s.command, s.enabled, s.status, s.last_error, "
    "s.created_at, s.updated_at, s.machine, s.owner_plugin"
)
_PLACEMENT_COLS = (
    "m.name IS NOT NULL AS machine_registered, m.paused_at, m.pause_reason, "
    "m.stopped_at, m.maintenance_reason, mp.online, mp.consecutive_failures"
)
_JOIN_CLAUSE = (
    "FROM schedules s "
    "LEFT JOIN machines m ON m.name = s.machine "
    "LEFT JOIN machine_probe mp ON mp.machine_name = s.machine"
)

_SUMMARY_QUERY = (
    f"SELECT {_SCHEDULE_BASE_COLS}, {_PLACEMENT_COLS} {_JOIN_CLAUSE} ORDER BY s.id DESC"
)
_FULL_QUERY = (
    f"SELECT {_SCHEDULE_BASE_COLS}, s.script, {_PLACEMENT_COLS} {_JOIN_CLAUSE} WHERE s.id = %s"
)
_MACHINE_PLACEMENT_QUERY = sql.SQL(
    "SELECT {} FROM (SELECT %s::text AS target) t "
    "LEFT JOIN machines m ON m.name = t.target "
    "LEFT JOIN machine_probe mp ON mp.machine_name = t.target"
).format(sql.SQL(_PLACEMENT_COLS))
_SCHEDULE_STORE_COLS = (
    "id, name, description, command, enabled, status, last_error, created_at, updated_at, "
    "machine, owner_plugin, script"
)

_DEFAULT_COMMAND = "python schedule.py"


class ScheduleSummary(BaseModel):
    id: int
    name: str
    description: str | None
    command: str
    enabled: bool
    status: str
    last_error: str | None
    created_at: datetime
    updated_at: datetime
    machine: str
    owner_plugin: str | None = None
    placement_status: PlacementStatus
    placement_reason: str | None = None


class ScheduleView(ScheduleSummary):
    script: str


class ScheduleCreate(BaseModel):
    name: str
    script: str
    command: str = _DEFAULT_COMMAND
    description: str | None = None
    enabled: bool = True
    machine: str | None = None


class ScheduleUpdate(BaseModel):
    name: str | None = None
    script: str | None = None
    command: str | None = None
    description: str | None = None
    enabled: bool | None = None
    machine: str | None = None


class ScheduleRunView(BaseModel):
    id: int
    ran_at: datetime
    ok: bool | None
    agent_id: int | None
    note: str | None


class ScheduleLogsView(BaseModel):
    source: str
    lines: list[str]


class ScheduleDraftRequest(BaseModel):
    nl: str = Field(description="Natural-language description of the scheduled task.")


class ScheduleDraftResponse(BaseModel):
    agent_id: int


def _snapshot(r: ScheduleSummaryRow | ScheduleFullRow) -> MachineSnapshot:
    return MachineSnapshot(
        registered=r.machine_registered,
        paused_at=r.paused_at,
        pause_reason=r.pause_reason,
        stopped_at=r.stopped_at,
        maintenance_reason=r.maintenance_reason,
        online=r.online,
        consecutive_failures=r.consecutive_failures or 0,
    )


def _summary(r: ScheduleSummaryRow) -> ScheduleSummary:
    placement = judge_placement(machine=r.machine, snapshot=_snapshot(r))
    return ScheduleSummary(
        id=r.id,
        name=r.name,
        description=r.description,
        command=r.command,
        enabled=r.enabled,
        status=r.status,
        last_error=r.last_error,
        created_at=r.created_at,
        updated_at=r.updated_at,
        machine=r.machine,
        owner_plugin=r.owner_plugin,
        placement_status=placement.status,
        placement_reason=placement.reason,
    )


def _view(r: ScheduleFullRow) -> ScheduleView:
    placement = judge_placement(machine=r.machine, snapshot=_snapshot(r))
    return ScheduleView(
        id=r.id,
        name=r.name,
        description=r.description,
        command=r.command,
        enabled=r.enabled,
        status=r.status,
        last_error=r.last_error,
        created_at=r.created_at,
        updated_at=r.updated_at,
        machine=r.machine,
        owner_plugin=r.owner_plugin,
        placement_status=placement.status,
        placement_reason=placement.reason,
        script=r.script,
    )


def _validate_script(script: str) -> None:
    try:
        compile(script, "<schedule>", "exec")
    except SyntaxError as exc:
        raise HTTPException(
            status_code=400, detail=f"script has a syntax error (line {exc.lineno}): {exc.msg}"
        ) from exc


def _edit_assignments(changed: dict[str, object], *, code_changed: bool) -> list[sql.Composable]:
    assignments: list[sql.Composable] = [
        sql.SQL("{} = %s").format(sql.Identifier(col)) for col in changed
    ]
    assignments.append(sql.SQL("updated_at = now()"))
    if code_changed or "enabled" in changed or "machine" in changed:
        assignments.extend(
            [
                sql.SQL("desired_revision = desired_revision + 1"),
                sql.SQL("launch_count = 0"),
                sql.SQL("next_launch_at = NULL"),
            ]
        )
    if "machine" in changed:
        assignments.append(sql.SQL("launch_generation = NULL"))
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
) -> None:
    code_changed = "script" in changed or "command" in changed
    assignments = _edit_assignments(changed, code_changed=code_changed)
    cur.execute(
        sql.SQL("UPDATE schedules SET {} WHERE id = %s").format(sql.SQL(", ").join(assignments)),
        (*changed.values(), schedule_id),
    )
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


def _sync_schedule_mutation(
    conn: psycopg.Connection,
    schedule_id: int,
    previous: ScheduleStoreRow,
    changed: dict[str, object],
) -> bool:
    old_machine: str = previous.machine if previous.machine else "gateway"
    new_val = changed.get("machine")
    new_machine: str = str(new_val) if new_val else old_machine
    machine_moved = "machine" in changed and changed["machine"] != previous.machine
    effective_enabled = changed.get("enabled", previous.enabled)
    needs_sync = plans.edit_needs_sync(changed, enabled=bool(effective_enabled))
    if needs_sync:
        if machine_moved:
            session_control.enqueue_in_transaction(conn, schedule_id, old_machine)
            session_control.enqueue_in_transaction(conn, schedule_id, new_machine)
        else:
            session_control.enqueue_in_transaction(conn, schedule_id, new_machine)
    return needs_sync


# meiva:link gateway/schedules/docs/router-contracts.meiva.okf.md
def update_blocking(
    pool: ConnectionPool,
    schedule_id: int,
    fields: dict[str, object],
    key: str | None = None,
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
            if not changed:
                full_row = query_one(conn, ScheduleFullRow, _FULL_QUERY, (schedule_id,))
                if full_row is None:
                    raise HTTPException(status_code=404, detail=f"schedule {schedule_id} not found")
                receipts.finish(conn, key, full_row)
                return full_row, False
            _apply_schedule_update(cur, schedule_id, previous, changed, fields)
            needs_sync = _sync_schedule_mutation(conn, schedule_id, previous, changed)
            row = query_one(conn, ScheduleFullRow, _FULL_QUERY, (schedule_id,))
            if row is None:
                raise HTTPException(status_code=404, detail=f"schedule {schedule_id} not found")
            receipts.finish(conn, key, row)
    except psycopg.errors.UniqueViolation as exc:
        raise HTTPException(
            status_code=409, detail=f"schedule named {fields['name']!r} already exists"
        ) from exc
    return row, needs_sync


# meiva:note
# The interpolated column list is a module constant; the values it filters on stay parameterized.
def _list_blocking(pool: ConnectionPool) -> list[ScheduleSummary]:
    with pool.connection() as conn:
        return [_summary(r) for r in query_all(conn, ScheduleSummaryRow, _SUMMARY_QUERY)]


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
) -> ScheduleFullRow:
    placement = query_one(conn, PlacementRow, _MACHINE_PLACEMENT_QUERY, (machine,))
    if placement is None:
        raise RuntimeError("LEFT JOIN against scalar target returned no row")
    return ScheduleFullRow(
        id=created.resource_id,
        name=body.name,
        description=body.description,
        command=body.command,
        enabled=body.enabled,
        status="stopped",
        last_error=None,
        created_at=created.created_at,
        updated_at=created.updated_at,
        machine=machine,
        owner_plugin=None,
        script=body.script,
        machine_registered=placement.machine_registered,
        paused_at=placement.paused_at,
        pause_reason=placement.pause_reason,
        stopped_at=placement.stopped_at,
        maintenance_reason=placement.maintenance_reason,
        online=placement.online,
        consecutive_failures=placement.consecutive_failures,
    )


# meiva:link gateway/schedules/docs/router-contracts.meiva.okf.md
def create_blocking(
    pool: ConnectionPool,
    body: ScheduleCreate,
    machine: str,
    key: str | None = None,
) -> ScheduleFullRow:
    _validate_script(body.script)
    try:
        with (
            write_transaction(pool) as conn,
            conn.cursor(row_factory=class_row(InsertedId)) as cur,
        ):
            created = creation_receipts.begin(
                conn, key, {"resource": "schedule", "body": body.model_dump(), "machine": machine}
            )
            if created is not None:
                return _reconstruct_created_row(conn, created, body, machine)
            cur.execute(
                "INSERT INTO schedules (name, description, script, command, enabled, machine) "
                "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
                (body.name, body.description, body.script, body.command, body.enabled, machine),
            )
            inserted = cur.fetchone()
            assert inserted is not None  # noqa: S101
            schedule_id = inserted.id
            cur.execute(
                "INSERT INTO schedule_versions (schedule_id, script, command, note) "
                "VALUES (%s, %s, %s, %s)",
                (schedule_id, body.script, body.command, "initial"),
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
    return row


@router.post(
    "/api/schedules",
    status_code=201,
    description="Create a schedule. 400 on a script syntax error, 409 on a name clash.",
)
async def create_schedule(request: Request, body: ScheduleCreate) -> ScheduleView:
    machine = (
        gateway_state(request).machine.name() if body.machine is None else body.machine.strip()
    )
    if not machine:
        raise HTTPException(status_code=400, detail="machine cannot be empty")
    row = await asyncio.to_thread(
        create_blocking,
        gateway_state(request).db_pool,
        body,
        machine,
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
    description="Partial-update a schedule — only the fields present in the body change. A\nscript/command change is snapshotted into schedule_versions and, if the\nschedule is enabled, applied immediately (the session is relaunched on the new\nscript). An enabled-flag value change also converges immediately, so disable\nreaps the session and enable creates it; a same-value update leaves the\nsession intact. 400 on a script syntax error, 404 if missing, 409 on a name\nclash.",
)
async def update_schedule(request: Request, schedule_id: int, body: ScheduleUpdate) -> ScheduleView:
    fields: dict[str, object] = body.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(status_code=400, detail="no fields to update")
    if "machine" in fields:
        machine = fields["machine"]
        if not isinstance(machine, str) or not machine.strip():
            raise HTTPException(status_code=400, detail="machine cannot be empty")
        fields["machine"] = machine.strip()
    row, needs_sync = await asyncio.to_thread(
        update_blocking,
        gateway_state(request).db_pool,
        schedule_id,
        fields,
        receipts.operation_key(request),
    )
    if needs_sync:
        await _wait_consumed(request, schedule_id)
    return _view(row)


def delete_blocking(pool: ConnectionPool, schedule_id: int) -> str:
    with write_transaction(pool) as conn:
        row = query_one(
            conn, OwnerRow, "SELECT owner_plugin FROM schedules WHERE id = %s", (schedule_id,)
        )
        if row is None:
            raise HTTPException(status_code=404, detail=f"schedule {schedule_id} not found")
        if row.owner_plugin is not None:
            raise HTTPException(
                status_code=409,
                detail=f"cannot delete schedule: owned by plugin '{row.owner_plugin}'",
            )
        deleted = query_one(
            conn,
            DeletedRow,
            "DELETE FROM schedules WHERE id = %s RETURNING machine",
            (schedule_id,),
        )
        if deleted is None:
            raise HTTPException(status_code=404, detail=f"schedule {schedule_id} not found")
        session_control.enqueue_in_transaction(conn, schedule_id, deleted.machine)
        return deleted.machine


async def _wait_consumed(request: Request, schedule_id: int, machine: str | None = None) -> None:
    config: GatewayConfig = gateway_state(request).gateway_config
    await session_control.wait_consumed(
        gateway_state(request).db_pool,
        schedule_id,
        machine=machine,
        wait_s=config.schedule_consume_wait_seconds,
        poll_s=config.schedule_consume_poll_seconds,
        timers=gateway_clock(request),
    )


def _remove_schedule_dir(home: Path, schedule_id: int) -> None:
    shutil.rmtree(home / "schedules" / str(schedule_id), ignore_errors=True)


@router.delete(
    "/api/schedules/{schedule_id}",
    description="Delete a schedule. 404 if missing. The reconcile loop kills its (now\norphaned) session; its work dir is removed.",
)
async def delete_schedule(request: Request, schedule_id: int) -> dict[str, str]:
    pool = gateway_state(request).db_pool
    machine = await asyncio.to_thread(delete_blocking, pool, schedule_id)
    await _wait_consumed(request, schedule_id, machine=machine)
    await asyncio.to_thread(
        _remove_schedule_dir, gateway_state(request).gateway_process.home, schedule_id
    )
    return {"status": "deleted"}


@router.post(
    "/api/schedules/{schedule_id}/start",
    description="Enable + launch the schedule now. 404 if missing.",
)
async def start_schedule(request: Request, schedule_id: int) -> ScheduleView:
    return await _control_and_sync(request, schedule_id, action="start")


@router.post(
    "/api/schedules/{schedule_id}/stop",
    description="Disable + kill the schedule's session now. 404 if missing.",
)
async def stop_schedule(request: Request, schedule_id: int) -> ScheduleView:
    return await _control_and_sync(request, schedule_id, action="stop")


@router.post(
    "/api/schedules/{schedule_id}/restart",
    description="Relaunch the schedule's session now (reloading its script), clearing crash\nbackoff. 404 if missing, 409 if the schedule is disabled.",
)
async def restart_schedule(request: Request, schedule_id: int) -> ScheduleView:
    return await _control_and_sync(request, schedule_id, action="restart")


def _read_transcript_blocking(
    backend: SessionBackend, schedule_id: int, lines: int
) -> list[str] | None:
    log_path = backend.session_log_path(session_name(f"schedule-{schedule_id}"))
    if log_path is None or not log_path.exists():
        return None
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    return text.splitlines()[-lines:]


@router.get(
    "/api/schedules/{schedule_id}/logs",
    description="Recent output of the schedule. Live session -> its capture; a dead\nsession -> its PTY transcript file; otherwise the last crash traceback\n(`last_error`). 404 if the schedule is missing.",
)
async def get_schedule_logs(
    request: Request, schedule_id: int, lines: int = 200
) -> ScheduleLogsView:

    row = await asyncio.to_thread(fetch_full_blocking, gateway_state(request).db_pool, schedule_id)
    target_machine = row.machine
    local_machine = gateway_state(request).machine.name()
    window = plans.capture_window(lines)

    if target_machine == local_machine:
        backend: SessionBackend = gateway_state(request).shell_backend
        captured = await session_control.capture(backend, schedule_id, window)
        transcript = (
            None
            if captured is not None
            else await asyncio.to_thread(_read_transcript_blocking, backend, schedule_id, window)
        )
    else:
        captured = None
        transcript = None
        try:
            result = await gateway_rpc(request).dispatch_to_machine(
                gateway_state(request).db,
                target_machine,
                "schedule_capture",
                {"schedule_id": schedule_id, "lines": window},
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
    return ScheduleLogsView(source=source, lines=shown)


def _runs_blocking(pool: ConnectionPool, schedule_id: int, limit: int) -> list[ScheduleRunView]:

    with pool.connection() as conn:
        return query_all(
            conn,
            ScheduleRunView,
            "SELECT id, ran_at, ok, agent_id, note FROM schedule_runs "
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


@router.post(
    "/api/keyed/v1/schedules/draft",
    description="Hand a natural-language request to an meiva-schedule-writer agent. Spawns the\nagent (which loads meiva-guide.schedules, clarifies, writes the script,\nand POSTs it back) and returns its id so the UI can open the conversation.\nReplays one raw draft intent and its original birth.",
)
async def guarded_draft_schedule(
    body: ScheduleDraftRequest, request: Request, key: str = Depends(guarded_draft_key)
) -> ScheduleDraftResponse:

    prompt = (
        "You are a schedule writer. Read and follow meiva.skills.meiva_guide.schedules to turn this "
        "request into a gateway-hosted schedule: clarify the trigger / skip / error-handling, "
        "write the script, and create it via POST /api/schedules. Request:\n\n" + body.nl
    )
    body_obj = SpawnAgentRequest(
        spawner="user",
        prompt=prompt,
        prompt_source="user",
        label="meiva-schedule-writer",
    )
    spawned = await create_and_launch_agent(
        body_obj,
        gateway_state(request).machine.name(),
        gateway_state(request).db_pool,
        gateway_state(request).db,
        gateway_state(request).bus,
        catalog=require_model_catalog(request),
        authority=gateway_state(request).config_authority,
        creation_key=key,
        clock=gateway_clock(request),
        rpc=gateway_rpc(request),
        creation_identity=body.model_dump(mode="json"),
    )
    return ScheduleDraftResponse(agent_id=spawned.id)


# meiva:link gateway/schedules/docs/router-contracts.meiva.okf.md
def fetch_full_blocking(pool: ConnectionPool, schedule_id: int) -> ScheduleFullRow:

    with pool.connection() as conn:
        row = query_one(conn, ScheduleFullRow, _FULL_QUERY, (schedule_id,))
    if row is None:
        raise HTTPException(status_code=404, detail=f"schedule {schedule_id} not found")
    return row


# meiva:link gateway/schedules/docs/router-contracts.meiva.okf.md
def control_blocking(
    pool: ConnectionPool, schedule_id: int, action: plans.ControlAction, key: str | None
) -> tuple[ScheduleFullRow, bool]:
    with write_transaction(pool) as conn:
        replay = receipts.begin(conn, key, {"schedule_id": schedule_id, "action": action})
        if replay is not None:
            return replay, False
        row = query_one(
            conn,
            ControlRow,
            "SELECT enabled, status, machine FROM schedules WHERE id = %s FOR UPDATE",
            (schedule_id,),
        )
        if row is None:
            raise HTTPException(status_code=404, detail=f"schedule {schedule_id} not found")
        machine = row.machine
        plan = plans.control_plan(action, enabled=row.enabled, status=row.status)
        if plan.refusal is not None:
            raise HTTPException(status_code=409, detail=plan.refusal)
        if plan.needs_sync:
            conn.execute(
                "UPDATE schedules SET enabled = %s, desired_revision = desired_revision + 1, "
                "launch_count = 0, next_launch_at = NULL, updated_at = now() WHERE id = %s",
                (plan.enabled, schedule_id),
            )
            session_control.enqueue_in_transaction(conn, schedule_id, machine)
        full_row = query_one(conn, ScheduleFullRow, _FULL_QUERY, (schedule_id,))
        assert full_row is not None  # noqa: S101
        receipts.finish(conn, key, full_row)
    return full_row, plan.needs_sync


async def _control_and_sync(
    request: Request, schedule_id: int, *, action: plans.ControlAction
) -> ScheduleView:
    pool = gateway_state(request).db_pool
    row, needs_sync = await asyncio.to_thread(
        control_blocking, pool, schedule_id, action, receipts.operation_key(request)
    )
    if needs_sync:
        await _wait_consumed(request, schedule_id)
    return _view(row)


def _launch_env_blocking(pool: ConnectionPool, schedule_id: int) -> tuple[bool, tuple[str, ...]]:
    with pool.connection() as conn:
        row = query_one(
            conn,
            LaunchEnvRow,
            "SELECT enabled, secret_env_vars FROM schedules WHERE id = %s",
            (schedule_id,),
        )
    if row is None:
        raise HTTPException(status_code=404, detail=f"schedule {schedule_id} not found")
    return row.enabled, tuple(row.secret_env_vars)


async def _read_gateway_secret_sources(
    request: Request,
) -> tuple[dict[str, str], dict[str, str]]:
    state = gateway_state(request)
    try:
        aliases = await asyncio.to_thread(state.config_authority.read_env_aliases)
    except OSError:
        aliases = {}
    process_env = state.gateway_process.config.process_environment()
    return dict(aliases), dict(process_env)


@router.get(
    "/api/schedules/{schedule_id}/launch-env",
    description="Deliver process-scoped secret environment variables for a schedule session.",
)
async def get_schedule_launch_env(
    request: Request,
    schedule_id: int,
    authorization: str | None = Header(default=None),
) -> dict[str, str]:
    secret = gateway_state(request).gateway_config.cluster_secret
    if (
        cluster_credential(
            authorization,
            secret,
            home=gateway_state(request).gateway_process.home,
            cache=gateway_state(request).machine_token_acceptance,
        )
        is None
    ):
        raise HTTPException(status_code=401, detail="machine API token required")

    pool = gateway_state(request).db_pool
    enabled, secret_env_vars = await asyncio.to_thread(_launch_env_blocking, pool, schedule_id)
    if not enabled or not secret_env_vars:
        return {}

    aliases, process_env = await _read_gateway_secret_sources(request)
    return plans.select_declared_secrets(secret_env_vars, aliases, process_env)

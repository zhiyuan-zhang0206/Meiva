from __future__ import annotations

import asyncio
import shutil
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, TypeGuard

import psycopg
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from psycopg import sql
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, Field

from base.cluster import session_name
from base.db.transaction import write_transaction
from base.sessions.protocol import SessionBackend
from gateway.agents.creation import create_and_launch_agent, guarded_draft_key
from gateway.http.auth.request_principal import cluster_credential
from gateway.http.config import GatewayConfig, gateway_clock, gateway_config, gateway_rpc
from gateway.http.model_catalog import require_model_catalog
from gateway.routers.receipts import creation as creation_receipts
from gateway.schedules import plans, receipts, session_control
from gateway.schedules.placement import MachineSnapshot, PlacementStatus, judge_placement
from gateway.schedules.receipts import PLACEMENT_COLS
from ops.cluster import rpc as cluster_rpc
from ops.rpc_schemas import ScheduleCaptureResult, SpawnAgentRequest

router = APIRouter()

_CAPTURE_TIMEOUT_S = 10.0

_SCHEDULE_BASE_COLS = (
    "s.id, s.name, s.description, s.command, s.enabled, s.status, s.last_error, "
    "s.created_at, s.updated_at, s.machine, s.owner_plugin"
)
_JOIN_CLAUSE = (
    "FROM schedules s "
    "LEFT JOIN machines m ON m.name = s.machine "
    "LEFT JOIN machine_probe mp ON mp.machine_name = s.machine"
)

_SUMMARY_QUERY = f"SELECT {_SCHEDULE_BASE_COLS}, {PLACEMENT_COLS} {_JOIN_CLAUSE} ORDER BY s.id DESC"
_FULL_QUERY = (
    f"SELECT {_SCHEDULE_BASE_COLS}, s.script, {PLACEMENT_COLS} {_JOIN_CLAUSE} WHERE s.id = %s"
)
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


def _summary(r: tuple[Any, ...]) -> ScheduleSummary:
    placement = judge_placement(
        machine=r[9],
        snapshot=MachineSnapshot(
            registered=bool(r[11]),
            paused_at=r[12],
            pause_reason=r[13],
            stopped_at=r[14],
            maintenance_reason=r[15],
            online=r[16],
            consecutive_failures=r[17] or 0,
        ),
    )
    return ScheduleSummary(
        id=r[0],
        name=r[1],
        description=r[2],
        command=r[3],
        enabled=r[4],
        status=r[5],
        last_error=r[6],
        created_at=r[7],
        updated_at=r[8],
        machine=r[9],
        owner_plugin=r[10],
        placement_status=placement.status,
        placement_reason=placement.reason,
    )


def _view(r: tuple[Any, ...]) -> ScheduleView:
    placement = judge_placement(
        machine=r[9],
        snapshot=MachineSnapshot(
            registered=bool(r[12]),
            paused_at=r[13],
            pause_reason=r[14],
            stopped_at=r[15],
            maintenance_reason=r[16],
            online=r[17],
            consecutive_failures=r[18] or 0,
        ),
    )
    return ScheduleView(
        id=r[0],
        name=r[1],
        description=r[2],
        command=r[3],
        enabled=r[4],
        status=r[5],
        last_error=r[6],
        created_at=r[7],
        updated_at=r[8],
        machine=r[9],
        owner_plugin=r[10],
        placement_status=placement.status,
        placement_reason=placement.reason,
        script=r[11],
    )


def _validate_script(script: str) -> None:
    try:
        compile(script, "<schedule>", "exec")
    except SyntaxError as exc:
        raise HTTPException(
            status_code=400, detail=f"script has a syntax error (line {exc.lineno}): {exc.msg}"
        ) from exc


def _edit_assignments(changed: dict[str, Any], *, code_changed: bool) -> list[sql.Composable]:
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


def _refuse_plugin_mutation(owner_plugin: str | None, changed: dict[str, Any]) -> None:
    if owner_plugin is not None:
        forbidden = set(changed) - {"enabled"}
        if forbidden:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"schedule is owned by plugin '{owner_plugin}'; "
                    "only enabled may be toggled directly"
                ),
            )


def _apply_schedule_update(
    cur: psycopg.Cursor[Any],
    schedule_id: int,
    previous: tuple[Any, ...],
    changed: dict[str, Any],
    fields: dict[str, Any],
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
                fields.get("script", previous[11]),
                fields.get("command", previous[3]),
                "edit",
            ),
        )


def _sync_schedule_mutation(
    conn: psycopg.Connection[Any],
    schedule_id: int,
    previous: tuple[Any, ...],
    changed: dict[str, Any],
) -> bool:
    old_machine: str = str(previous[9]) if previous[9] else "gateway"
    new_val = changed.get("machine")
    new_machine: str = str(new_val) if new_val else old_machine
    machine_moved = "machine" in changed and changed["machine"] != previous[9]
    effective_enabled = changed.get("enabled", previous[4])
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
    pool: ConnectionPool[Any],
    schedule_id: int,
    fields: dict[str, Any],
    key: str | None = None,
) -> tuple[tuple[Any, ...], bool]:

    if "script" in fields:
        _validate_script(fields["script"])

    try:
        with write_transaction(pool) as conn, conn.cursor() as cur:
            replay = receipts.begin(conn, key, {"schedule_id": schedule_id, "fields": fields})
            if replay is not None:
                return replay, False
            cur.execute(
                sql.SQL("SELECT {} FROM schedules WHERE id = %s FOR UPDATE").format(
                    sql.SQL(_SCHEDULE_STORE_COLS)
                ),
                (schedule_id,),
            )
            previous = cur.fetchone()
            if previous is None:
                raise HTTPException(status_code=404, detail=f"schedule {schedule_id} not found")
            changed = plans.changed_fields(previous, fields)
            _refuse_plugin_mutation(previous[10], changed)
            if not changed:
                cur.execute(_FULL_QUERY, (schedule_id,))
                full_row = cur.fetchone()
                if full_row is None:
                    raise HTTPException(status_code=404, detail=f"schedule {schedule_id} not found")
                receipts.finish(conn, key, full_row)
                return full_row, False
            _apply_schedule_update(cur, schedule_id, previous, changed, fields)
            needs_sync = _sync_schedule_mutation(conn, schedule_id, previous, changed)
            cur.execute(_FULL_QUERY, (schedule_id,))
            row = cur.fetchone()
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
def _list_blocking(pool: ConnectionPool[Any]) -> list[ScheduleSummary]:

    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(_SUMMARY_QUERY)
        return [_summary(r) for r in cur.fetchall()]


@router.get(
    "/api/schedules",
    description="List all schedules (enabled and disabled), newest first. Omits `script`.",
)
async def list_schedules(request: Request) -> list[ScheduleSummary]:

    return await asyncio.to_thread(_list_blocking, request.app.state.db_pool)


# meiva:link gateway/schedules/docs/router-contracts.meiva.okf.md
def create_blocking(
    pool: ConnectionPool[Any],
    body: ScheduleCreate,
    machine: str,
    key: str | None = None,
) -> tuple[Any, ...]:

    _validate_script(body.script)
    try:
        with write_transaction(pool) as conn, conn.cursor() as cur:
            created = creation_receipts.begin(
                conn, key, {"resource": "schedule", "body": body.model_dump(), "machine": machine}
            )
            if created is not None:
                return receipts.creation_replay_row(
                    cur,
                    created,
                    name=body.name,
                    description=body.description,
                    command=body.command,
                    enabled=body.enabled,
                    script=body.script,
                    machine=machine,
                )
            cur.execute(
                "INSERT INTO schedules (name, description, script, command, enabled, machine) "
                "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
                (body.name, body.description, body.script, body.command, body.enabled, machine),
            )
            inserted = cur.fetchone()
            assert inserted is not None  # noqa: S101
            schedule_id = inserted[0]
            cur.execute(
                "INSERT INTO schedule_versions (schedule_id, script, command, note) "
                "VALUES (%s, %s, %s, %s)",
                (schedule_id, body.script, body.command, "initial"),
            )
            cur.execute(_FULL_QUERY, (schedule_id,))
            row = cur.fetchone()
            assert row is not None  # noqa: S101
            creation_receipts.finish(conn, key, creation_receipts.Creation(row[0], row[7], row[8]))
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

    machine = request.app.state.machine.name() if body.machine is None else body.machine.strip()
    if not machine:
        raise HTTPException(status_code=400, detail="machine cannot be empty")

    row = await asyncio.to_thread(
        create_blocking,
        request.app.state.db_pool,
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

    row = await asyncio.to_thread(fetch_full_blocking, request.app.state.db_pool, schedule_id)
    return _view(row)


@router.put(
    "/api/schedules/{schedule_id}",
    description="Partial-update a schedule — only the fields present in the body change. A\nscript/command change is snapshotted into schedule_versions and, if the\nschedule is enabled, applied immediately (the session is relaunched on the new\nscript). An enabled-flag value change also converges immediately, so disable\nreaps the session and enable creates it; a same-value update leaves the\nsession intact. 400 on a script syntax error, 404 if missing, 409 on a name\nclash.",
)
async def update_schedule(request: Request, schedule_id: int, body: ScheduleUpdate) -> ScheduleView:

    fields = body.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(status_code=400, detail="no fields to update")

    if "machine" in fields:
        if not isinstance(fields["machine"], str) or not fields["machine"].strip():
            raise HTTPException(status_code=400, detail="machine cannot be empty")
        fields["machine"] = fields["machine"].strip()

    row, needs_sync = await asyncio.to_thread(
        update_blocking,
        request.app.state.db_pool,
        schedule_id,
        fields,
        receipts.operation_key(request),
    )
    if needs_sync:
        await _wait_consumed(request, schedule_id)
    return _view(row)


def delete_blocking(pool: ConnectionPool[Any], schedule_id: int) -> str:

    with write_transaction(pool) as conn, conn.cursor() as cur:
        cur.execute("SELECT owner_plugin FROM schedules WHERE id = %s", (schedule_id,))
        row = cur.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail=f"schedule {schedule_id} not found")
        if row[0] is not None:
            raise HTTPException(
                status_code=409,
                detail=f"cannot delete schedule: owned by plugin '{row[0]}'",
            )
        cur.execute("DELETE FROM schedules WHERE id = %s RETURNING id, machine", (schedule_id,))
        del_row = cur.fetchone()
        if del_row is None:
            raise HTTPException(status_code=404, detail=f"schedule {schedule_id} not found")
        machine = del_row[1]
        session_control.enqueue_in_transaction(conn, schedule_id, machine)
        return machine


async def _wait_consumed(request: Request, schedule_id: int, machine: str | None = None) -> None:
    config: GatewayConfig = request.app.state.gateway_config
    await session_control.wait_consumed(
        request.app.state.db_pool,
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

    pool = request.app.state.db_pool
    machine = await asyncio.to_thread(delete_blocking, pool, schedule_id)
    await _wait_consumed(request, schedule_id, machine=machine)
    await asyncio.to_thread(
        _remove_schedule_dir, request.app.state.gateway_process.home, schedule_id
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

    row = await asyncio.to_thread(fetch_full_blocking, request.app.state.db_pool, schedule_id)
    target_machine = row[9]
    local_machine = request.app.state.machine.name()
    window = plans.capture_window(lines)

    if target_machine == local_machine:
        backend: SessionBackend = request.app.state.shell_backend
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
                request.app.state.db,
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
        lines, captured=captured, transcript=transcript, last_error=row[6]
    )
    return ScheduleLogsView(source=source, lines=shown)


def _runs_blocking(
    pool: ConnectionPool[Any], schedule_id: int, limit: int
) -> list[ScheduleRunView]:

    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, ran_at, ok, agent_id, note FROM schedule_runs "
            "WHERE schedule_id = %s ORDER BY ran_at DESC, id DESC LIMIT %s",
            (schedule_id, limit),
        )
        return [
            ScheduleRunView(id=r[0], ran_at=r[1], ok=r[2], agent_id=r[3], note=r[4])
            for r in cur.fetchall()
        ]


@router.get(
    "/api/schedules/{schedule_id}/runs",
    description="Recent run-history rows (newest first). Omit `limit` for the configured\ndefault window (``display.schedules_runs_default_limit`` - 50 out of the\nbox). 404 if the schedule is missing.",
)
async def get_schedule_runs(
    request: Request, schedule_id: int, limit: int | None = None
) -> list[ScheduleRunView]:

    if limit is None:
        limit = gateway_config(request).display.schedules_runs_default_limit
    pool = request.app.state.db_pool
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
        request.app.state.machine.name(),
        request.app.state.db_pool,
        request.app.state.db,
        request.app.state.bus,
        catalog=require_model_catalog(request),
        authority=request.app.state.config_authority,
        creation_key=key,
        clock=gateway_clock(request),
        rpc=gateway_rpc(request),
        creation_identity=body.model_dump(mode="json"),
    )
    return ScheduleDraftResponse(agent_id=spawned.id)


# meiva:link gateway/schedules/docs/router-contracts.meiva.okf.md
def fetch_full_blocking(pool: ConnectionPool[Any], schedule_id: int) -> tuple[Any, ...]:

    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(_FULL_QUERY, (schedule_id,))
        row = cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"schedule {schedule_id} not found")
    return row


# meiva:link gateway/schedules/docs/router-contracts.meiva.okf.md
def control_blocking(
    pool: ConnectionPool[Any], schedule_id: int, action: plans.ControlAction, key: str | None
) -> tuple[tuple[Any, ...], bool]:
    with write_transaction(pool) as conn, conn.cursor() as cur:
        replay = receipts.begin(conn, key, {"schedule_id": schedule_id, "action": action})
        if replay is not None:
            return replay, False
        cur.execute(
            "SELECT id, enabled, status, machine FROM schedules WHERE id = %s FOR UPDATE",
            (schedule_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail=f"schedule {schedule_id} not found")
        _, enabled, status, machine = row[0], row[1], row[2], row[3]
        plan = plans.control_plan(action, enabled=enabled, status=status)
        if plan.refusal is not None:
            raise HTTPException(status_code=409, detail=plan.refusal)
        if plan.needs_sync:
            cur.execute(
                "UPDATE schedules SET enabled = %s, desired_revision = desired_revision + 1, "
                "launch_count = 0, next_launch_at = NULL, updated_at = now() WHERE id = %s",
                (plan.enabled, schedule_id),
            )
            session_control.enqueue_in_transaction(conn, schedule_id, machine)
        cur.execute(_FULL_QUERY, (schedule_id,))
        full_row = cur.fetchone()
        assert full_row is not None  # noqa: S101
        receipts.finish(conn, key, full_row)
    return full_row, plan.needs_sync


async def _control_and_sync(
    request: Request, schedule_id: int, *, action: plans.ControlAction
) -> ScheduleView:
    pool = request.app.state.db_pool
    row, needs_sync = await asyncio.to_thread(
        control_blocking, pool, schedule_id, action, receipts.operation_key(request)
    )
    if needs_sync:
        await _wait_consumed(request, schedule_id)
    return _view(row)


def _launch_env_blocking(
    pool: ConnectionPool[Any], schedule_id: int
) -> tuple[bool, tuple[str, ...]]:
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT enabled, secret_env_vars FROM schedules WHERE id = %s",
            (schedule_id,),
        )
        row = cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"schedule {schedule_id} not found")
    enabled = bool(row[0])
    raw_secrets: object = row[1]
    secrets_list: list[str] = []
    if _is_sequence(raw_secrets):
        for item in raw_secrets:
            if isinstance(item, str):
                secrets_list.append(item)
    return enabled, tuple(secrets_list)


def _is_sequence(val: object) -> TypeGuard[Sequence[object]]:
    return isinstance(val, (list, tuple))


def _is_dict(value: object) -> TypeGuard[dict[object, object]]:
    return isinstance(value, dict)


def _read_env_aliases_sync(config_authority: object) -> dict[str, str]:
    read_fn = getattr(config_authority, "read_env_aliases", None)
    if not callable(read_fn):
        return {}
    try:
        raw_aliases: object = read_fn()
    except OSError:
        return {}
    if not _is_dict(raw_aliases):
        return {}
    return {k: v for k, v in raw_aliases.items() if isinstance(k, str) and isinstance(v, str)}


def _read_process_env(process: object) -> dict[str, str]:
    config = getattr(process, "config", None)
    if config is None:
        return {}
    proc_env_fn = getattr(config, "process_environment", None)
    if not callable(proc_env_fn):
        return {}
    raw_env: object = proc_env_fn()
    if not _is_dict(raw_env):
        return {}
    return {k: v for k, v in raw_env.items() if isinstance(k, str) and isinstance(v, str)}


async def _read_gateway_secret_sources(
    request: Request,
) -> tuple[dict[str, str], dict[str, str]]:
    authority = getattr(request.app.state, "config_authority", None)
    aliases: dict[str, str] = (
        await asyncio.to_thread(_read_env_aliases_sync, authority) if authority is not None else {}
    )
    process = getattr(request.app.state, "gateway_process", None)
    process_env = _read_process_env(process) if process is not None else {}
    return aliases, process_env


@router.get(
    "/api/schedules/{schedule_id}/launch-env",
    description="Deliver process-scoped secret environment variables for a schedule session.",
)
async def get_schedule_launch_env(
    request: Request,
    schedule_id: int,
    authorization: str | None = Header(default=None),
) -> dict[str, str]:
    secret = request.app.state.gateway_config.cluster_secret
    if (
        cluster_credential(
            authorization,
            secret,
            home=request.app.state.gateway_process.home,
            cache=request.app.state.machine_token_acceptance,
        )
        is None
    ):
        raise HTTPException(status_code=401, detail="machine API token required")

    pool = request.app.state.db_pool
    enabled, secret_env_vars = await asyncio.to_thread(_launch_env_blocking, pool, schedule_id)
    if not enabled or not secret_env_vars:
        return {}

    aliases, process_env = await _read_gateway_secret_sources(request)
    return plans.select_declared_secrets(secret_env_vars, aliases, process_env)

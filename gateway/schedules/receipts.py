"""Schedule mutation identities committed with desired state and queued work."""

from datetime import datetime
from typing import Any

import psycopg
from fastapi import HTTPException, Request
from psycopg import Connection
from psycopg.types.json import Jsonb

from gateway.http.auth.request_principal import PrincipalScopeError, optional_request_key
from gateway.routers.receipts import creation as creation_receipts


def operation_key(request: Request) -> str | None:
    try:
        key = optional_request_key(request)
    except PrincipalScopeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return None if key is None else f"{request.method}:{request.url.path}:{key}"


def begin(
    conn: Connection[Any], key: str | None, payload: dict[str, Any]
) -> tuple[Any, ...] | None:
    if key is None:
        return None
    conn.execute(
        "INSERT INTO schedule_operation_receipts (operation_key, request) VALUES (%s, %s) "
        "ON CONFLICT DO NOTHING",
        (key, Jsonb(payload)),
    )
    row = conn.execute(
        "SELECT request, response FROM schedule_operation_receipts WHERE operation_key = %s FOR UPDATE",
        (key,),
    ).fetchone()
    assert row is not None  # noqa: S101 — insert or conflicting committed receipt
    if row[0] != payload:
        raise HTTPException(
            status_code=409,
            detail="idempotency key was already used for a different schedule request",
        )
    return None if row[1] is None else tuple(row[1])


def finish(conn: Connection[Any], key: str | None, row: tuple[Any, ...]) -> None:
    if key is not None:
        values = [value.isoformat() if isinstance(value, datetime) else value for value in row]
        conn.execute(
            "UPDATE schedule_operation_receipts SET response = %s WHERE operation_key = %s",
            (Jsonb(values), key),
        )


# meiva:note
# The placement facts every schedule row carries: what the schedule's machine is registered as,
# and whether that machine is paused, stopped, online. They describe the machine, not the
# schedule, so a reader that answers for a schedule which no longer exists still reads them live.
PLACEMENT_COLS = (
    "m.name IS NOT NULL AS machine_registered, m.paused_at, m.pause_reason, "
    "m.stopped_at, m.maintenance_reason, mp.online, mp.consecutive_failures"
)

# meiva:note
# Those columns for one machine name, for the replay that has no schedule row to join from.
# The interpolated list is a module constant and the machine name is a bound parameter, which is
# the whole of what could be injected here.
PLACEMENT_OF_MACHINE_QUERY = (
    f"SELECT {PLACEMENT_COLS} FROM (SELECT %s::text AS machine) p "  # noqa: S608
    "LEFT JOIN machines m ON m.name = p.machine "
    "LEFT JOIN machine_probe mp ON mp.machine_name = p.machine"
)


# meiva:note
# A same-key creation retry answers the original creation representation, built from the
# receipt's resource id and timestamps plus the body it replays. It never reads the schedule row:
# a rename, later edit, deletion or name reuse cannot turn an old acceptance into a new resource,
# and replaying after a delete still answers 201 while GET answers 404
# (gateway/routers/docs/resource-creation.meiva.okf.md). The two queries it runs return one row
# each, so the placement read can never come back empty.
def creation_replay_row(
    cur: psycopg.Cursor[Any],
    created: creation_receipts.Creation,
    *,
    name: str,
    description: str | None,
    command: str,
    enabled: bool,
    script: str,
    machine: str,
) -> tuple[Any, ...]:

    cur.execute(PLACEMENT_OF_MACHINE_QUERY, (machine,))
    placement = cur.fetchone()
    assert placement is not None  # noqa: S101
    return (
        created.resource_id,
        name,
        description,
        command,
        enabled,
        "stopped",
        None,
        created.created_at,
        created.updated_at,
        machine,
        None,
        script,
        *placement,
    )

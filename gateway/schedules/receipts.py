"""Schedule mutation identities committed with desired state and queued work."""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from fastapi import HTTPException, Request
from psycopg import Connection
from psycopg.types.json import Jsonb
from pydantic import TypeAdapter

from base.db.rows import query_one
from base.host.boundary.json_values import is_object_list
from gateway.http.auth.request_principal import PrincipalScopeError, optional_request_key
from gateway.schedules.rows import ScheduleFullRow


def operation_key(request: Request) -> str | None:
    try:
        key = optional_request_key(request)
    except PrincipalScopeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return None if key is None else f"{request.method}:{request.url.path}:{key}"


# meiva:note
# A keyed operation's stored request and response (the response is null until `finish`), both jsonb.
@dataclass(frozen=True)
class _ReceiptRow:
    request: object
    response: object


# meiva:note
# The stored response is the full row's values in field order, datetimes as ISO strings; reading it
# back as the row class is validation, so a receipt of a different shape fails here.
_FULL_ROW = TypeAdapter(ScheduleFullRow)


def begin(conn: Connection, key: str | None, payload: dict[str, object]) -> ScheduleFullRow | None:
    if key is None:
        return None
    conn.execute(
        "INSERT INTO schedule_operation_receipts (operation_key, request) VALUES (%s, %s) "
        "ON CONFLICT DO NOTHING",
        (key, Jsonb(payload)),
    )
    row = query_one(
        conn,
        _ReceiptRow,
        "SELECT request, response FROM schedule_operation_receipts WHERE operation_key = %s FOR UPDATE",
        (key,),
    )
    assert row is not None  # noqa: S101 — insert or conflicting committed receipt
    if row.request != payload:
        raise HTTPException(
            status_code=409,
            detail="idempotency key was already used for a different schedule request",
        )
    if row.response is None:
        return None
    if not is_object_list(row.response):
        raise TypeError("stored schedule receipt is not a list")
    return _FULL_ROW.validate_python(row.response)


def _json_values(values: Iterable[object]) -> list[object]:
    return [value.isoformat() if isinstance(value, datetime) else value for value in values]


def finish(conn: Connection, key: str | None, row: ScheduleFullRow) -> None:
    if key is not None:
        values = _json_values(row)
        conn.execute(
            "UPDATE schedule_operation_receipts SET response = %s WHERE operation_key = %s",
            (Jsonb(values), key),
        )

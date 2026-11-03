"""User settings endpoints — persistent key-value store for frontend preferences.

GET /api/settings — list all settings.
PUT /api/settings/{key} — upsert a single setting.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from fastapi import APIRouter, Request
from psycopg.types.json import Jsonb

from base.db.rows import query_all, query_one
from base.db.transaction import write_transaction
from gateway.http.config import gateway_state
from gateway.schemas import (
    UserSettingListResponse,
    UserSettingRow,
    UserSettingUpdateRequest,
)

router = APIRouter()


# meiva:note
# One `user_settings` row; `value` is the stored jsonb.
@dataclass(frozen=True)
class _SettingDbRow:
    key: str
    value: object
    updated_at: datetime


def _setting_row(row: _SettingDbRow) -> UserSettingRow:
    return UserSettingRow(key=row.key, value=row.value, updated_at=row.updated_at.isoformat())


@router.get("/api/settings")
def get_settings(request: Request) -> UserSettingListResponse:
    """Return every row in the user_settings table."""
    with gateway_state(request).db_pool.connection() as conn:
        settings = [
            _setting_row(r)
            for r in query_all(
                conn, _SettingDbRow, "SELECT key, value, updated_at FROM user_settings ORDER BY key"
            )
        ]
    return UserSettingListResponse(settings=settings)


@router.put("/api/settings/{key}")
def put_setting(key: str, body: UserSettingUpdateRequest, request: Request) -> UserSettingRow:
    """Upsert a single setting by key."""
    with write_transaction(gateway_state(request).db_pool) as conn:
        row = query_one(
            conn,
            _SettingDbRow,
            "INSERT INTO user_settings (key, value, updated_at) "
            "VALUES (%s, %s, now()) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now() "
            "RETURNING key, value, updated_at",
            (key, Jsonb(body.value)),
        )
        assert row is not None  # noqa: S101 — upsert RETURNING always yields a row
    return _setting_row(row)

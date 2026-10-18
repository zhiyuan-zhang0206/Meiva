from __future__ import annotations

from datetime import datetime
from typing import Any, cast

from psycopg_pool import AsyncConnectionPool, ConnectionPool

from base.db.transaction import async_write_transaction, write_transaction

SETTINGS_KEY = "notifications.categories"

_SELECT_OVERRIDES = "SELECT value FROM user_settings WHERE key = %s"
_SELECT_LAST_KEYED = (
    "SELECT created_at FROM notification_events "
    "WHERE category = %s AND dedupe_key = %s ORDER BY created_at DESC LIMIT 1"
)
_INSERT = (
    "INSERT INTO notification_events "
    "(category, subject, body, href, dedupe_key, importance, agent_id) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id"
)


def parse_overrides(value: Any) -> dict[str, bool]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise TypeError(f"{SETTINGS_KEY} must be a JSON object")
    entries = cast("dict[str, object]", value)
    switches: dict[str, bool] = {}
    for key, item in entries.items():
        if not isinstance(item, bool):
            raise TypeError(f"{SETTINGS_KEY}.{key} must be a bool")
        switches[key] = item
    return switches


def _last(row: tuple[Any, ...] | None) -> datetime | None:
    return None if row is None else row[0]


def read_inputs_sync(
    pool: ConnectionPool, *, category: str, dedupe_key: str | None
) -> tuple[dict[str, bool], datetime | None]:
    with write_transaction(pool) as conn:
        overrides = parse_overrides(
            (conn.execute(_SELECT_OVERRIDES, (SETTINGS_KEY,)).fetchone() or (None,))[0]
        )
        last = (
            None
            if dedupe_key is None
            else _last(conn.execute(_SELECT_LAST_KEYED, (category, dedupe_key)).fetchone())
        )
    return overrides, last


async def read_inputs_async(
    pool: AsyncConnectionPool[Any], *, category: str, dedupe_key: str | None
) -> tuple[dict[str, bool], datetime | None]:
    async with async_write_transaction(pool) as conn:
        row = await (await conn.execute(_SELECT_OVERRIDES, (SETTINGS_KEY,))).fetchone()
        overrides = parse_overrides(None if row is None else row[0])
        last = (
            None
            if dedupe_key is None
            else _last(
                await (await conn.execute(_SELECT_LAST_KEYED, (category, dedupe_key))).fetchone()
            )
        )
    return overrides, last


def insert_sync(pool: ConnectionPool, params: tuple[Any, ...]) -> int:
    with write_transaction(pool) as conn:
        row = conn.execute(_INSERT, params).fetchone()
    if row is None:
        raise RuntimeError("notification insert returned no id")
    return int(row[0])


async def insert_async(pool: AsyncConnectionPool[Any], params: tuple[Any, ...]) -> int:
    async with async_write_transaction(pool) as conn:
        row = await (await conn.execute(_INSERT, params)).fetchone()
    if row is None:
        raise RuntimeError("notification insert returned no id")
    return int(row[0])


def read_overrides_sync(pool: ConnectionPool) -> dict[str, bool]:
    with pool.connection() as conn:
        row = conn.execute(_SELECT_OVERRIDES, (SETTINGS_KEY,)).fetchone()
    return parse_overrides(None if row is None else row[0])

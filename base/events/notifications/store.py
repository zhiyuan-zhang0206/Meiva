from __future__ import annotations

from datetime import datetime
from typing import Any, cast

import psycopg
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
# meiva:note
# One row still owed its live event, by id: `announced_at IS NULL` is the whole
# outbox condition — the producer committed the delivery, the transport never
# accepted the publication. The pending scan is oldest first and never takes a row
# a producer staged moments ago, so a sweep cannot duplicate an announce that is
# still in flight.
# meiva:link base/events/notifications/docs/notifications.meiva.okf.md
_SELECT_UNANNOUNCED = (
    "SELECT id, category, subject, body, href, importance, agent_id "
    "FROM notification_events WHERE id = %s AND announced_at IS NULL"
)
_SELECT_PENDING = (
    "SELECT id FROM notification_events "
    "WHERE announced_at IS NULL AND created_at < now() - make_interval(secs => %s) "
    "ORDER BY id LIMIT %s"
)
_MARK_ANNOUNCED = (
    "UPDATE notification_events SET announced_at = now() WHERE id = %s AND announced_at IS NULL"
)
# meiva:note
# The recovery read: the newest deliveries after a client cursor, so a client that
# was offline catches up instead of losing the live event. Newest-first because
# the cap must drop the OLDEST row of a long gap, not the most recent one.
# meiva:link base/events/notifications/docs/notifications.meiva.okf.md
_SELECT_RECENT = (
    "SELECT id, category, subject, body, href, importance, agent_id, created_at "
    "FROM notification_events WHERE id > %s ORDER BY id DESC LIMIT %s"
)
_SELECT_LATEST = "SELECT COALESCE(MAX(id), 0) FROM notification_events"


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


def read_inputs(
    conn: psycopg.Connection, *, category: str, dedupe_key: str | None
) -> tuple[dict[str, bool], datetime | None]:
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


def insert(conn: psycopg.Connection, params: tuple[Any, ...]) -> int:
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


def unannounced_sync(pool: ConnectionPool, notification_id: int) -> tuple[Any, ...] | None:
    with pool.connection() as conn:
        return conn.execute(_SELECT_UNANNOUNCED, (notification_id,)).fetchone()


async def unannounced_async(
    pool: AsyncConnectionPool[Any], notification_id: int
) -> tuple[Any, ...] | None:
    async with async_write_transaction(pool) as conn:
        return await (await conn.execute(_SELECT_UNANNOUNCED, (notification_id,))).fetchone()


def mark_announced_sync(pool: ConnectionPool, notification_id: int) -> bool:
    with write_transaction(pool) as conn:
        return conn.execute(_MARK_ANNOUNCED, (notification_id,)).rowcount > 0


async def mark_announced_async(pool: AsyncConnectionPool[Any], notification_id: int) -> bool:
    async with async_write_transaction(pool) as conn:
        cursor = await conn.execute(_MARK_ANNOUNCED, (notification_id,))
    return cursor.rowcount > 0


def pending_sync(pool: ConnectionPool, *, min_age_seconds: float, limit: int) -> list[int]:
    with pool.connection() as conn:
        rows = conn.execute(_SELECT_PENDING, (min_age_seconds, limit)).fetchall()
    return [int(row[0]) for row in rows]


def recent_sync(pool: ConnectionPool, *, after_id: int, limit: int) -> list[tuple[Any, ...]]:
    with pool.connection() as conn:
        rows = conn.execute(_SELECT_RECENT, (after_id, limit)).fetchall()
    return list(reversed(rows))


def latest_id_sync(pool: ConnectionPool) -> int:
    with pool.connection() as conn:
        row = conn.execute(_SELECT_LATEST).fetchone()
    return 0 if row is None else int(row[0])

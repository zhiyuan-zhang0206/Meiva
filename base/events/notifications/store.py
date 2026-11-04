from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TypeGuard

import psycopg
from psycopg.rows import class_row
from psycopg_pool import ConnectionPool

from base.db.transaction import write_transaction

SETTINGS_KEY = "notifications.categories"

_SELECT_OVERRIDES = "SELECT value FROM user_settings WHERE key = %s"
_SELECT_INPUT_BASE = (
    "SELECT (SELECT value FROM user_settings WHERE key = %s) AS overrides, now() AS now"
)
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
_SELECT_LATEST = "SELECT COALESCE(MAX(id), 0) AS id FROM notification_events"


def _is_object_dict(value: object) -> TypeGuard[dict[str, object]]:
    return isinstance(value, dict)


def parse_overrides(value: object) -> dict[str, bool]:
    if value is None:
        return {}
    if not _is_object_dict(value):
        raise TypeError(f"{SETTINGS_KEY} must be a JSON object")
    switches: dict[str, bool] = {}
    for key, item in value.items():
        if not isinstance(item, bool):
            raise TypeError(f"{SETTINGS_KEY}.{key} must be a bool")
        switches[key] = item
    return switches


# meiva:note
# The single column of each read below, by name: `class_row` builds one of these per row.
@dataclass(frozen=True)
class _InputBase:
    overrides: object
    now: datetime


@dataclass(frozen=True)
class _CreatedAt:
    created_at: datetime


@dataclass(frozen=True)
class _Value:
    value: object


@dataclass(frozen=True)
class _Id:
    id: int


# meiva:note
# One row still owed its live event, as `unannounced_sync` returns it (the columns of `NotificationRecord`).
@dataclass(frozen=True)
class UnannouncedRow:
    id: int
    category: str
    subject: str
    body: str | None
    href: str | None
    importance: str
    agent_id: int


def read_inputs(
    conn: psycopg.Connection, *, category: str, dedupe_key: str | None
) -> tuple[dict[str, bool], datetime | None, datetime]:
    with conn.cursor(row_factory=class_row(_InputBase)) as cur:
        base_row = cur.execute(_SELECT_INPUT_BASE, (SETTINGS_KEY,)).fetchone()
    if base_row is None:
        raise RuntimeError("notification input query returned no row")
    overrides = parse_overrides(base_row.overrides)
    last = None
    if dedupe_key is not None:
        with conn.cursor(row_factory=class_row(_CreatedAt)) as cur:
            keyed = cur.execute(_SELECT_LAST_KEYED, (category, dedupe_key)).fetchone()
        last = None if keyed is None else keyed.created_at
    return overrides, last, base_row.now


def insert(conn: psycopg.Connection, params: tuple[object, ...]) -> int:
    with conn.cursor(row_factory=class_row(_Id)) as cur:
        row = cur.execute(_INSERT, params).fetchone()
    if row is None:
        raise RuntimeError("notification insert returned no id")
    return row.id


def read_overrides_sync(pool: ConnectionPool) -> dict[str, bool]:
    with pool.connection() as conn, conn.cursor(row_factory=class_row(_Value)) as cur:
        row = cur.execute(_SELECT_OVERRIDES, (SETTINGS_KEY,)).fetchone()
    return parse_overrides(None if row is None else row.value)


def unannounced_sync(pool: ConnectionPool, notification_id: int) -> UnannouncedRow | None:
    with pool.connection() as conn, conn.cursor(row_factory=class_row(UnannouncedRow)) as cur:
        return cur.execute(_SELECT_UNANNOUNCED, (notification_id,)).fetchone()


def mark_announced_sync(pool: ConnectionPool, notification_id: int) -> bool:
    with write_transaction(pool) as conn:
        return conn.execute(_MARK_ANNOUNCED, (notification_id,)).rowcount > 0


def pending_sync(pool: ConnectionPool, *, min_age_seconds: float, limit: int) -> list[int]:
    with pool.connection() as conn, conn.cursor(row_factory=class_row(_Id)) as cur:
        rows = cur.execute(_SELECT_PENDING, (min_age_seconds, limit)).fetchall()
    return [row.id for row in rows]


# meiva:note
# One delivered notification as the recovery read returns it.
@dataclass(frozen=True)
class NotificationDelivery:
    id: int
    category: str
    subject: str
    body: str | None
    href: str | None
    importance: str
    agent_id: int
    created_at: datetime


def recent_sync(pool: ConnectionPool, *, after_id: int, limit: int) -> list[NotificationDelivery]:
    with pool.connection() as conn, conn.cursor(row_factory=class_row(NotificationDelivery)) as cur:
        cur.execute(_SELECT_RECENT, (after_id, limit))
        rows = cur.fetchall()
    return list(reversed(rows))


def latest_id_sync(pool: ConnectionPool) -> int:
    with pool.connection() as conn, conn.cursor(row_factory=class_row(_Id)) as cur:
        row = cur.execute(_SELECT_LATEST).fetchone()
    return 0 if row is None else row.id

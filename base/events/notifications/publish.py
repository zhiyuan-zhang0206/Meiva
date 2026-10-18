from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import psycopg
from psycopg_pool import AsyncConnectionPool, ConnectionPool

from base.db.transaction import write_transaction
from base.events.live.bus import EventBus
from base.events.live.projection import NotificationEmitted
from base.events.notifications.catalog import NotificationCategory, core_catalog
from base.events.notifications.policy import decide
from base.events.notifications.store import (
    insert,
    insert_async,
    mark_announced_async,
    mark_announced_sync,
    read_inputs,
    read_inputs_async,
    unannounced_async,
    unannounced_sync,
)

_log = logging.getLogger("base.events.notifications")


@dataclass(frozen=True)
class NotificationRecord:
    id: int
    category: str
    subject: str
    body: str | None
    href: str | None
    importance: str
    agent_id: int


def category(
    category_id: str, catalog: Mapping[str, NotificationCategory] | None = None
) -> NotificationCategory:
    declared = (catalog if catalog is not None else core_catalog()).get(category_id)
    if declared is None:
        raise ValueError(f"unknown notification category: {category_id}")
    return declared


def _record(
    category_id: str,
    *,
    subject: str,
    body: str | None,
    href: str | None,
    dedupe_key: str | None,
    agent_id: int,
    overrides: dict[str, bool] | None,
    now: datetime | None,
    inputs: tuple[dict[str, bool], datetime | None],
    catalog: Mapping[str, NotificationCategory] | None = None,
) -> tuple[NotificationCategory, tuple[Any, ...]] | None:
    declared = category(category_id, catalog)
    if not subject.strip():
        raise ValueError(f"notification {declared.id} needs a subject")
    stored_overrides, last = inputs
    switches = stored_overrides if overrides is None else overrides
    decision = decide(
        declared, overrides=switches, last_same_key_at=last, now=now or datetime.now(UTC)
    )
    if not decision.deliver:
        _log.debug("notification %s suppressed: %s", declared.id, decision.reason)
        return None
    params = (
        declared.id,
        subject,
        body,
        href,
        dedupe_key,
        declared.importance,
        agent_id,
    )
    return declared, params


# meiva:note
# The producing transaction stages its own delivery: a notice that needs an answer
# commits its `notification_events` row with the notice, so the debt exists before
# any process could die. `announced_at` stays NULL until the transport accepts the
# publication, which is why the dedupe lookup counts an unannounced row too -- the
# outbox sweep converges it to delivered rather than a second delivery row.
# meiva:link base/events/notifications/docs/notifications.meiva.okf.md
def deliver_in_transaction(
    conn: psycopg.Connection,
    *,
    category_id: str,
    subject: str,
    body: str | None = None,
    href: str | None = None,
    dedupe_key: str | None = None,
    agent_id: int = 0,
    overrides: dict[str, bool] | None = None,
    now: datetime | None = None,
    catalog: Mapping[str, NotificationCategory] | None = None,
) -> NotificationRecord | None:
    declared = category(category_id, catalog)
    inputs = read_inputs(conn, category=declared.id, dedupe_key=dedupe_key)
    prepared = _record(
        category_id,
        subject=subject,
        body=body,
        href=href,
        dedupe_key=dedupe_key,
        agent_id=agent_id,
        overrides=overrides,
        now=now,
        inputs=inputs,
        catalog=catalog,
    )
    if prepared is None:
        return None
    declared, params = prepared
    return NotificationRecord(
        id=insert(conn, params),
        category=declared.id,
        subject=subject,
        body=body,
        href=href,
        importance=declared.importance,
        agent_id=agent_id,
    )


def deliver_sync(
    pool: ConnectionPool,
    *,
    category_id: str,
    subject: str,
    body: str | None = None,
    href: str | None = None,
    dedupe_key: str | None = None,
    agent_id: int = 0,
    overrides: dict[str, bool] | None = None,
    now: datetime | None = None,
    catalog: Mapping[str, NotificationCategory] | None = None,
) -> NotificationRecord | None:
    with write_transaction(pool) as conn:
        return deliver_in_transaction(
            conn,
            category_id=category_id,
            subject=subject,
            body=body,
            href=href,
            dedupe_key=dedupe_key,
            agent_id=agent_id,
            overrides=overrides,
            now=now,
            catalog=catalog,
        )


async def deliver_async(
    pool: AsyncConnectionPool[Any],
    *,
    category_id: str,
    subject: str,
    body: str | None = None,
    href: str | None = None,
    dedupe_key: str | None = None,
    agent_id: int = 0,
    overrides: dict[str, bool] | None = None,
    now: datetime | None = None,
    catalog: Mapping[str, NotificationCategory] | None = None,
) -> NotificationRecord | None:
    declared = category(category_id, catalog)
    inputs = await read_inputs_async(pool, category=declared.id, dedupe_key=dedupe_key)
    prepared = _record(
        category_id,
        subject=subject,
        body=body,
        href=href,
        dedupe_key=dedupe_key,
        agent_id=agent_id,
        overrides=overrides,
        now=now,
        inputs=inputs,
        catalog=catalog,
    )
    if prepared is None:
        return None
    declared, params = prepared
    return NotificationRecord(
        id=await insert_async(pool, params),
        category=declared.id,
        subject=subject,
        body=body,
        href=href,
        importance=declared.importance,
        agent_id=agent_id,
    )


def _emitted(
    notification_id: int,
    category: str,
    subject: str,
    body: str | None,
    href: str | None,
    importance: str,
    agent_id: int,
) -> NotificationEmitted:
    return NotificationEmitted(
        agent_id=agent_id,
        notification_id=notification_id,
        category=category,
        subject=subject,
        body=body,
        href=href,
        importance=importance,
    )


# meiva:note
# Announcing is one step with two outcomes: the transport accepted the live event
# and the row is marked `announced_at`, or the publish failed (the transport
# returns None for a known Redis/network failure) and the row stays unannounced
# for the outbox sweep. A crash between publish and mark re-announces — an
# at-least-once push the client already deduplicates on `notification_id` — and is
# never a lost one.
# meiva:link base/events/notifications/docs/notifications.meiva.okf.md
async def announce(
    pool: AsyncConnectionPool[Any], bus: EventBus | None, notification_id: int
) -> bool:
    if bus is None:
        return False
    row = await unannounced_async(pool, notification_id)
    if row is None:
        return False
    published = await bus.publish_best_effort(
        _emitted(*row).model_dump_json(), context="notification_emitted"
    )
    if published is None:
        return False
    return await mark_announced_async(pool, notification_id)


def announce_sync(pool: ConnectionPool, bus: EventBus | None, notification_id: int) -> bool:
    if bus is None:
        return False
    row = unannounced_sync(pool, notification_id)
    if row is None:
        return False
    published = bus.publish_best_effort_sync(
        _emitted(*row).model_dump_json(), context="notification_emitted"
    )
    if published is None:
        return False
    return mark_announced_sync(pool, notification_id)


# meiva:note
# Delivery is the only side effect: one row in `notification_events` plus one
# live `notification` event. Suppression returns None, so a producer cannot tell a
# switched-off category from a deduplicated one and has nothing to retry.
# meiva:link base/events/notifications/docs/notifications.meiva.okf.md
async def publish_notification(
    pool: AsyncConnectionPool[Any],
    bus: EventBus | None,
    **fields: Any,
) -> int | None:
    record = await deliver_async(pool, **fields)
    if record is None:
        return None
    await announce(pool, bus, record.id)
    return record.id

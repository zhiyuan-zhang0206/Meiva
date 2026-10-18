from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from psycopg_pool import AsyncConnectionPool, ConnectionPool

from base.events.live.bus import EventBus
from base.events.live.projection import NotificationEmitted
from base.events.notifications.catalog import CORE_CATEGORIES, NotificationCategory
from base.events.notifications.policy import decide
from base.events.notifications.store import (
    insert_async,
    insert_sync,
    read_inputs_async,
    read_inputs_sync,
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


def category(category_id: str) -> NotificationCategory:
    for declared in CORE_CATEGORIES:
        if declared.id == category_id:
            return declared
    raise ValueError(f"unknown notification category: {category_id}")


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
) -> tuple[NotificationCategory, tuple[Any, ...]] | None:
    declared = category(category_id)
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
) -> NotificationRecord | None:
    declared = category(category_id)
    inputs = read_inputs_sync(pool, category=declared.id, dedupe_key=dedupe_key)
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
    )
    if prepared is None:
        return None
    declared, params = prepared
    return NotificationRecord(
        id=insert_sync(pool, params),
        category=declared.id,
        subject=subject,
        body=body,
        href=href,
        importance=declared.importance,
        agent_id=agent_id,
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
) -> NotificationRecord | None:
    declared = category(category_id)
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


async def announce(bus: EventBus | None, record: NotificationRecord) -> None:
    if bus is None:
        return
    event = NotificationEmitted(
        agent_id=record.agent_id,
        notification_id=record.id,
        category=record.category,
        subject=record.subject,
        body=record.body,
        href=record.href,
        importance=record.importance,
    )
    await bus.publish_best_effort(event.model_dump_json(), context="notification_emitted")


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
    await announce(bus, record)
    return record.id

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from psycopg_pool import AsyncConnectionPool, ConnectionPool

from base.events.live.bus import EventBus
from base.events.live.projection import NotificationEmitted
from base.events.notifications.catalog import NotificationCategory, core_catalog
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
    declared = category(category_id, catalog)
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
        catalog=catalog,
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


def _emitted(record: NotificationRecord) -> NotificationEmitted:
    return NotificationEmitted(
        agent_id=record.agent_id,
        notification_id=record.id,
        category=record.category,
        subject=record.subject,
        body=record.body,
        href=record.href,
        importance=record.importance,
    )


async def announce(bus: EventBus | None, record: NotificationRecord) -> None:
    if bus is None:
        return
    await bus.publish_best_effort(
        _emitted(record).model_dump_json(), context="notification_emitted"
    )


# meiva:note
# The sync-context counterpart of `announce`: a producer outside the event loop
# (the task-maintenance daemon) publishes the same one live event, so no producer
# has to reach for a weaker delivery.
# meiva:link base/events/notifications/docs/notifications.meiva.okf.md
def announce_sync(bus: EventBus | None, record: NotificationRecord) -> None:
    if bus is None:
        return
    bus.publish_best_effort_sync(_emitted(record).model_dump_json(), context="notification_emitted")


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

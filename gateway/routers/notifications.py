from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel

from base.events.notifications.store import latest_id_sync, recent_sync
from gateway.http.config import gateway_state

router = APIRouter()


class NotificationDeliveryItem(BaseModel):
    id: int
    category: str
    action: str
    tag: str
    subject: str
    body: str | None
    href: str | None
    importance: str
    agent_id: int
    created_at: datetime


class NotificationDeliveriesResponse(BaseModel):
    notifications: list[NotificationDeliveryItem]
    latest_id: int


# meiva:note
# The recovery read a client needs after it was offline: the live `notification`
# event is a Redis publication, so a client that missed it has no second chance
# unless it can ask the delivery log. It returns the NEWEST `limit` deliveries
# after the cursor, oldest-first, plus the newest id in the table so a first-run
# client can set its cursor without replaying history. The category switches and
# deduplication were already applied when these rows were written; this endpoint
# re-decides nothing.
# meiva:link base/events/notifications/README.md
@router.get("/api/notifications")
async def get_notifications(
    request: Request,
    after_id: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 10,
) -> NotificationDeliveriesResponse:
    pool = gateway_state(request).db_pool
    rows, latest_id = await asyncio.gather(
        asyncio.to_thread(recent_sync, pool, after_id=after_id, limit=limit),
        asyncio.to_thread(latest_id_sync, pool),
    )
    return NotificationDeliveriesResponse(
        notifications=[
            NotificationDeliveryItem(
                id=row.id,
                category=row.category,
                action=row.action,
                tag=row.tag,
                subject=row.subject,
                body=row.body,
                href=row.href,
                importance=row.importance,
                agent_id=row.agent_id,
                created_at=row.created_at,
            )
            for row in rows
        ],
        latest_id=latest_id,
    )

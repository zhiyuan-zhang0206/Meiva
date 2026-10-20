from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from base.clock import Clock
from base.events.notifications.audit import run_audit
from base.events.notifications.publish import announce_sync, deliver_sync
from base.events.notifications.store import latest_id_sync, read_overrides_sync, recent_sync
from gateway.extensions.notification_categories import declared_categories

router = APIRouter()


class NotificationCreateIn(BaseModel):
    category: str
    subject: str
    body: str | None = None
    href: str | None = None
    dedupe_key: str | None = None


class NotificationCategoryItem(BaseModel):
    id: str
    label: str
    description: str
    importance: str
    enabled: bool


class NotificationCategoriesResponse(BaseModel):
    categories: list[NotificationCategoryItem]


class AuditWindow(BaseModel):
    start: str
    end: str
    days: int
    timezone: str


class AuditDay(BaseModel):
    day: str
    by_category: dict[str, int]
    total: int


class AuditDelivered(BaseModel):
    total: int
    per_day: list[AuditDay]
    per_category: dict[str, int]
    per_hour: list[int]


class AuditCandidates(BaseModel):
    category: str
    candidates: int
    delivered: int
    suppressed: int
    repeat_deliveries: int


class AuditSimulation(BaseModel):
    variant: str
    delivered: int
    delta: int
    per_day: float


class AuditExplained(BaseModel):
    at: str
    category: str
    agent_id: int
    dedupe_key: str | None
    outcome: str


class NotificationAuditResponse(BaseModel):
    window: AuditWindow
    switches: dict[str, bool]
    categories: list[str]
    sources: dict[str, str]
    delivered: AuditDelivered
    candidates: list[AuditCandidates]
    simulations: list[AuditSimulation]
    explain: list[AuditExplained] | None = None


class NotificationDeliveryItem(BaseModel):
    id: int
    category: str
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
# The merged catalog with the user's switch already applied, so the console
# renders exactly what the spine will honor.
# meiva:link base/events/notifications/docs/notifications.meiva.okf.md
@router.get("/api/notifications/categories")
async def get_notification_categories(request: Request) -> NotificationCategoriesResponse:
    pool = request.app.state.db_pool
    catalog = await asyncio.to_thread(declared_categories)
    switches = await asyncio.to_thread(read_overrides_sync, pool)
    return NotificationCategoriesResponse(
        categories=[
            NotificationCategoryItem(
                id=declared.id,
                label=declared.label,
                description=declared.description,
                importance=declared.importance,
                enabled=switches.get(declared.id, declared.default_enabled),
            )
            for declared in catalog.values()
        ]
    )


# meiva:note
# `delivered: false` means the spine suppressed the notification — the category is
# switched off, or the same `(category, dedupe_key)` fired inside the dedupe
# window. The caller has nothing to retry.
# meiva:link base/events/notifications/docs/notifications.meiva.okf.md
@router.post("/api/agents/{agent_id}/notifications", status_code=201)
async def post_notification(
    agent_id: int, body: NotificationCreateIn, request: Request
) -> dict[str, Any]:
    pool = request.app.state.db_pool
    catalog = await asyncio.to_thread(declared_categories)
    if body.category not in catalog:
        raise HTTPException(
            status_code=422, detail=f"unknown notification category: {body.category}"
        )
    try:
        record = await asyncio.to_thread(
            deliver_sync,
            pool,
            category_id=body.category,
            subject=body.subject,
            body=body.body,
            href=body.href,
            dedupe_key=body.dedupe_key,
            agent_id=agent_id,
            catalog=catalog,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if record is None:
        return {"delivered": False, "id": None}
    await asyncio.to_thread(announce_sync, pool, request.app.state.bus, record.id)
    return {"delivered": True, "id": record.id}


# meiva:note
# The recovery read a client needs after it was offline: the live `notification`
# event is a Redis publication, so a client that missed it has no second chance
# unless it can ask the delivery log. It returns the NEWEST `limit` deliveries
# after the cursor, oldest-first, plus the newest id in the table so a first-run
# client can set its cursor without replaying history. The category switches and
# deduplication were already applied when these rows were written; this endpoint
# re-decides nothing.
# meiva:link base/events/notifications/docs/notifications.meiva.okf.md
@router.get("/api/notifications")
async def get_notifications(
    request: Request,
    after_id: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 10,
) -> NotificationDeliveriesResponse:
    pool = request.app.state.db_pool
    rows, latest_id = await asyncio.gather(
        asyncio.to_thread(recent_sync, pool, after_id=after_id, limit=limit),
        asyncio.to_thread(latest_id_sync, pool),
    )
    return NotificationDeliveriesResponse(
        notifications=[
            NotificationDeliveryItem(
                id=int(row[0]),
                category=row[1],
                subject=row[2],
                body=row[3],
                href=row[4],
                importance=row[5],
                agent_id=int(row[6]),
                created_at=row[7],
            )
            for row in rows
        ],
        latest_id=latest_id,
    )


# meiva:note
# A read-only replay of the producers over a window, run server-side so the
# operator CLI and any other client see the same answer: the candidates come from
# the notice rows, the switches from the stored overrides and the declared catalog
# from the installed manifests. A bad window, zone, category or simulation is a 422;
# `tz` defaults to the cluster clock's configured zone, never the caller's.
# meiva:link base/events/notifications/docs/notifications.meiva.okf.md
@router.get("/api/notifications/audit")
async def get_notification_audit(
    request: Request,
    *,
    days: Annotated[int, Query(ge=1)] = 7,
    category: str | None = None,
    tz: str | None = None,
    now: datetime | None = None,
    simulate: Annotated[list[str] | None, Query()] = None,
    explain: bool = False,
) -> NotificationAuditResponse:
    if now is not None and now.tzinfo is None:
        raise HTTPException(status_code=422, detail="now needs a UTC offset")
    pool = request.app.state.db_pool
    catalog = await asyncio.to_thread(declared_categories)
    try:
        report = await asyncio.to_thread(
            run_audit,
            pool,
            catalog=catalog,
            days=days,
            category=category,
            tz_name=tz or Clock.from_settings().timezone,
            now=now,
            simulate=simulate or (),
            explain=explain,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return NotificationAuditResponse.model_validate(report)

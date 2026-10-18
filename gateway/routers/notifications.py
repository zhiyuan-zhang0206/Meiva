from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from base.events.notifications.publish import announce, deliver_sync
from base.events.notifications.store import read_overrides_sync
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
    await announce(request.app.state.bus, record)
    return {"delivered": True, "id": record.id}

import asyncio
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from base.agents.to_user.delivery.store import (
    DeliveryConflictError,
    advance_seen,
    delivery_mode,
    messages,
    publish,
    seen_id,
)
from base.api_contracts.user_delivery import (
    MarkSeen,
    PublishMessage,
    SeenResult,
    UserDeliveryMode,
    UserMessage,
)
from base.db.transaction import write_transaction
from base.events.live.announce import publish_agent_updated
from gateway.agents.eval_guard import deny_isolated_result_read
from gateway.agents.notice_operations.keyed_operation import operation_key
from gateway.agents.notice_operations.notices import existing_receipt, save_receipt
from gateway.http.config import gateway_clock, gateway_config, gateway_state

router = APIRouter()


class UserMessageFeed(BaseModel):
    mode: UserDeliveryMode
    seen_id: int
    items: list[UserMessage]


@router.get(
    "/api/agents/{agent_id}/user-messages", dependencies=[Depends(deny_isolated_result_read)]
)
async def get_messages(
    agent_id: int,
    request: Request,
    *,
    pending: bool = False,
    after_id: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int | None, Query(ge=1, le=100)] = None,
) -> UserMessageFeed:
    def read() -> UserMessageFeed:
        with gateway_state(request).db_pool.connection() as conn:
            mode = delivery_mode(conn, agent_id, gateway_config(request).user_delivery_mode)
            return UserMessageFeed(
                mode=mode,
                seen_id=seen_id(conn, agent_id),
                items=messages(
                    conn, agent_id, pending_only=pending, after_id=after_id, limit=limit
                ),
            )

    try:
        return await asyncio.to_thread(read)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post("/api/agents/{agent_id}/user-messages", status_code=201)
async def post_message(
    agent_id: int,
    body: PublishMessage,
    request: Request,
    key: Annotated[str, Depends(operation_key)],
) -> UserMessage:
    now = gateway_clock(request).now()

    def write() -> UserMessage:
        with write_transaction(gateway_state(request).db_pool) as conn:
            mode = delivery_mode(conn, agent_id, gateway_config(request).user_delivery_mode)
            if mode is not UserDeliveryMode.SDK:
                raise HTTPException(409, "meiva.ui.publish requires user_delivery_mode=sdk")
            return publish(conn, agent_id, f"sdk:{key}", body, mode=mode, created_at=now)

    try:
        result = await asyncio.to_thread(write)
    except DeliveryConflictError as exc:
        raise HTTPException(409, str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    await publish_agent_updated(gateway_state(request).bus, agent_id)
    return result


@router.post("/api/keyed/v1/agents/{agent_id}/user-messages/seen")
async def mark_seen(
    agent_id: int,
    body: MarkSeen,
    request: Request,
    key: Annotated[str, Depends(operation_key)],
) -> SeenResult:
    payload = body.model_dump(mode="json")

    def write() -> SeenResult:
        with write_transaction(gateway_state(request).db_pool) as conn:
            previous = existing_receipt(conn, request.url.path, key, payload)
            if previous is not None:
                return SeenResult.model_validate(previous)
            cursor, advanced = advance_seen(conn, agent_id, body.up_to_id)
            result = SeenResult(seen_id=cursor, advanced=advanced)
            save_receipt(conn, request.url.path, key, payload, result.model_dump(mode="json"))
            return result

    try:
        result = await asyncio.to_thread(write)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if result.advanced:
        await publish_agent_updated(gateway_state(request).bus, agent_id)
    return result

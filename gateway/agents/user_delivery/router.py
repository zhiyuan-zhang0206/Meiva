import asyncio
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from psycopg import Connection, sql
from psycopg.rows import dict_row
from pydantic import BaseModel

from base.agents.impersonation.wake import publish_inbound_wake
from base.agents.messages.inbound import InboundKind
from base.agents.to_user.delivery.store import (
    COLUMNS,
    DeliveryConflictError,
    delivery_mode,
    messages,
    publish,
)
from base.api_contracts.user_delivery import (
    MessageAction,
    PublishMessage,
    ResolveMessage,
    UserDeliveryMode,
    UserMessage,
)
from base.db.transaction import write_transaction
from base.events.live.announce import publish_agent_updated
from gateway.agents.eval_guard import deny_isolated_result_read
from gateway.agents.inbound_provenance import request_inbound_provenance
from gateway.agents.notice_operations.current import operation_key
from gateway.agents.notice_operations.notices import resolve_once
from gateway.agents.schemas import AgentMessageEnqueued
from gateway.http.config import gateway_clock, gateway_config, gateway_rpc, gateway_state
from ops import lifecycle
from ops.agents import get_agent_status

router = APIRouter()


class UserMessageFeed(BaseModel):
    mode: UserDeliveryMode
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


def apply_action(
    conn: Connection, agent_id: int, message_id: int, body: ResolveMessage
) -> str | None:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            sql.SQL(
                "SELECT {} FROM agent_user_messages WHERE agent_id=%s AND id=%s FOR UPDATE"
            ).format(sql.SQL(COLUMNS)),
            (agent_id, message_id),
        )
        raw = cur.fetchone()
        if raw is None:
            raise HTTPException(404, "user message does not exist")
        message = UserMessage.model_validate(raw)
        if message.content is None:
            raise HTTPException(409, "a no-update decision is not a user message")
        if body.action is MessageAction.READ:
            cur.execute(
                "UPDATE agent_user_messages SET read_at=COALESCE(read_at,now()) WHERE id=%s",
                (message_id,),
            )
            return None
        if message.resolved_at is not None:
            raise HTTPException(409, "this user message has already been resolved")
        resolution = "answered" if body.action is MessageAction.ANSWER else "dismissed"
        cur.execute(
            "UPDATE agent_user_messages SET read_at=COALESCE(read_at,now()), resolved_at=now(), "
            "resolution=%s, reply=%s WHERE id=%s",
            (resolution, body.reply, message_id),
        )
        if body.action is MessageAction.ANSWER:
            return f"User reply to delivery #{message_id}:\n\n{message.content}\n\nReply:\n{body.reply}"
        return f"The user closed delivery #{message_id} without answering. This grants no authorization.\n\n{message.content}"


@router.post("/api/keyed/v1/agents/{agent_id}/user-messages/{message_id}/resolve")
async def resolve_message(
    agent_id: int,
    message_id: int,
    body: ResolveMessage,
    request: Request,
    key: Annotated[str, Depends(operation_key)],
) -> AgentMessageEnqueued:
    source = "user" if body.action is MessageAction.ANSWER else "system:user-delivery-dismiss"
    receipt = await asyncio.to_thread(
        resolve_once,
        gateway_state(request).db_pool,
        request.url.path,
        key,
        body.model_dump(mode="json"),
        agent_id,
        source,
        lambda conn: apply_action(conn, agent_id, message_id, body),
        request_inbound_provenance(request),
    )
    inbound_id = receipt["inbound_id"]
    if inbound_id is not None:
        if not isinstance(inbound_id, int):
            raise TypeError("delivery resolution receipt has an invalid inbound id")
        await asyncio.to_thread(
            publish_inbound_wake,
            gateway_state(request).db,
            gateway_state(request).bus,
            agent_id,
            str(inbound_id),
        )
        content = receipt["content"]
        if not isinstance(content, str):
            raise TypeError("delivery receipt has invalid content")
        await lifecycle.publish_inbound_arrived(
            gateway_state(request).bus, agent_id, inbound_id, "chat", source, content
        )
        status = await lifecycle.resurrect_if_terminated(
            gateway_state(request).db,
            gateway_state(request).bus,
            agent_id,
            trigger_inbound_id=inbound_id,
            trigger_inbound_kind=InboundKind.CHAT,
            local_machine=gateway_state(request).machine.name(),
            rpc=gateway_rpc(request),
        )
    else:
        status = await asyncio.to_thread(get_agent_status, gateway_state(request).db, agent_id)
    await publish_agent_updated(gateway_state(request).bus, agent_id)
    return AgentMessageEnqueued(status=status, inbound_id=inbound_id)

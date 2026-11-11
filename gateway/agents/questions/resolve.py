# meiva:note
# The question resolution: one answer or dismissal, settled with its agent inbound in one transaction.
# meiva:link gateway/routers/docs/frontend-ui-data.md

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

import psycopg
from fastapi import APIRouter, Header, HTTPException, Request
from psycopg.rows import class_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from base.agents.inbox.insert import insert_inbound_message_in_transaction
from base.agents.inbox.wake import publish_inbound_wake
from base.agents.messages.inbound import InboundKind
from base.agents.messages.inbound_provenance import InboundProvenance
from base.db.transaction import write_transaction
from base.events.notifications.publish import stage_question_resolved
from gateway.agents.inbound_provenance import request_inbound_provenance
from gateway.agents.keyed_operations import existing_receipt, keyed_request_key, save_receipt
from gateway.agents.questions.answers import resolution_answer, resolution_inbound
from gateway.agents.schemas import (
    AgentMessageEnqueued,
    ResolveQuestionIn,
)
from gateway.http.config import gateway_rpc, gateway_state
from ops import lifecycle as _ops
from ops.agents import get_agent_status

router = APIRouter()


# meiva:note
# The columns a resolution reads under `FOR UPDATE`; `question` is the stored jsonb, parsed by
# `resolution_answer`.
@dataclass(frozen=True)
class _ResolveTarget:
    title: str
    resolved_at: datetime | None
    question: object


# meiva:note
# `_ACTION_RESOLUTION` maps an action to its stored resolution explicitly, never
# inferred from reply presence. The caps bound one response's payload; the
# configurable surface is the *default* window (user ruling 2026-09-17 / #3696).
# meiva:link gateway/routers/docs/frontend-ui-data.md
_ACTION_RESOLUTION = {"answer": "answered", "dismiss": "dismissed"}


async def post_question_resolve(
    agent_id: int,
    question_id: int,
    body: ResolveQuestionIn,
    request: Request,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=128),
) -> AgentMessageEnqueued:
    # meiva:note
    # Resolution and reply delivery share one durable receipt. A dismissal is a
    # question-system event, never user authorization or ordinary user speech.
    # meiva:link agent/messages/docs/message-format.md
    idempotency_key = keyed_request_key(request, idempotency_key)
    action = body.action
    reply = body.reply

    def _resolve(conn: psycopg.Connection) -> str | None:
        if action == "read":
            raise HTTPException(
                status_code=422, detail="questions require a reply or an explicit dismissal"
            )
        with conn.cursor(row_factory=class_row(_ResolveTarget)) as cur:
            cur.execute(
                "SELECT title, resolved_at, question "
                "FROM agent_questions WHERE id = %s AND agent_id = %s FOR UPDATE",
                (question_id, agent_id),
            )
            target = cur.fetchone()
            if target is None:
                raise HTTPException(
                    status_code=409,
                    detail=f"question {question_id} does not exist for agent {agent_id}",
                )
            resolved_answer = resolution_answer(body, target.question)
            reply = resolved_answer.reply
            if target.resolved_at is not None:
                raise HTTPException(
                    status_code=409,
                    detail=f"question {question_id} is already resolved for agent {agent_id}",
                )
            if action == "answer" and reply is None:
                raise HTTPException(status_code=422, detail="action 'answer' requires a reply")
            cur.execute(
                "UPDATE agent_questions SET resolved_at = now(), resolution = %s, reply = %s, answer = %s "
                "WHERE id = %s",
                (
                    _ACTION_RESOLUTION[action],
                    reply,
                    Jsonb(resolved_answer.answer.model_dump(mode="json"))
                    if resolved_answer.answer is not None
                    else None,
                    question_id,
                ),
            )
            stage_question_resolved(
                conn, agent_id=agent_id, question_id=question_id, title=target.title
            )
        return resolution_inbound(target.title, action, resolved_answer.reply)

    # meiva:note
    # Every question resolution rides the question-system source (reply / dismiss); the claim node
    # wraps both as a system note, so no User-role message is consumed.
    # meiva:link agent/messages/docs/message-format.md
    deliver_source = (
        "system:question-reply"
        if reply is not None or body.answer is not None
        else "system:question-dismiss"
    )
    receipt = await asyncio.to_thread(
        resolve_once,
        gateway_state(request).db_pool,
        request.url.path,
        idempotency_key,
        body.model_dump(mode="json", exclude={"answer"} if body.answer is None else set()),
        agent_id,
        deliver_source,
        _resolve,
        request_inbound_provenance(request),
    )
    inbound_id = receipt["inbound_id"]
    if inbound_id is not None:
        if not isinstance(inbound_id, int):
            raise TypeError("question inbound receipt must be an integer")
        await asyncio.to_thread(
            publish_inbound_wake,
            gateway_state(request).db,
            gateway_state(request).bus,
            agent_id,
            str(inbound_id),
        )
        content = receipt["content"]
        if not isinstance(content, str):
            raise TypeError("question inbound content must be a string")
        await _ops.publish_inbound_arrived(
            gateway_state(request).bus,
            agent_id,
            inbound_id,
            "chat",
            deliver_source,
            content,
        )
        status = await _ops.resurrect_if_terminated(
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
    await _ops.publish_question_resolved(gateway_state(request).bus, agent_id, question_id)
    return AgentMessageEnqueued(status=status, inbound_id=inbound_id)


# meiva:note
# Resolve and insert the reply with one durable operation receipt.
def resolve_once(
    pool: ConnectionPool,
    path: str,
    key: str,
    body: dict[str, object],
    agent_id: int,
    source: str,
    prepare: Callable[[psycopg.Connection], str | None],
    provenance: InboundProvenance,
) -> dict[str, object]:
    with write_transaction(pool) as conn:
        previous = existing_receipt(conn, path, key, body)
        if previous is not None:
            return previous
        content = prepare(conn)
        inbound_id = None
        if content is not None:
            with conn.cursor() as cur:
                inbound_id, _ = insert_inbound_message_in_transaction(
                    cur,
                    agent_id,
                    content,
                    source,
                    provenance=provenance,
                )
        receipt: dict[str, object] = {"inbound_id": inbound_id, "content": content}
        save_receipt(conn, path, key, body, receipt)
    return receipt

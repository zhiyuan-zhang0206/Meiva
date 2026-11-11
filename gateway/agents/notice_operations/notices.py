# meiva:note
# The question surface: resolve a question and read the unresolved ones.
# meiva:link gateway/routers/docs/frontend-ui-data.md

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated

import psycopg
from fastapi import APIRouter, Header, HTTPException, Query, Request
from psycopg.rows import class_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from base.agents.inbox.insert import insert_inbound_message_in_transaction
from base.agents.inbox.wake import publish_inbound_wake
from base.agents.messages.inbound import InboundKind
from base.agents.messages.inbound_provenance import InboundProvenance
from base.db.transaction import write_transaction
from base.host.boundary.json_values import is_object_dict
from gateway.agents.inbound_provenance import request_inbound_provenance
from gateway.agents.notice_operations.notice_answers import resolution_answer, resolution_inbound
from gateway.agents.notice_operations.notice_plan import (
    question_expired,
)
from gateway.agents.schemas import (
    AgentMessageEnqueued,
    NoticeItem,
    NoticesFeed,
    ResolveNoticeIn,
)
from gateway.http.auth.request_principal import PrincipalScopeError, request_key
from gateway.http.config import gateway_clock, gateway_config, gateway_rpc, gateway_state
from ops import lifecycle as _ops
from ops.agents import get_agent_status

router = APIRouter()


# meiva:note
# The columns a resolution reads under `FOR UPDATE`; `question` is the stored jsonb, parsed by
# `resolution_answer`.
@dataclass(frozen=True)
class _ResolveTarget:
    title: str
    require_response: bool
    resolved_at: datetime | None
    question: object
    expire_at: datetime | None
    created_at: datetime


# meiva:note
# A notice operation's stored request and receipt, both `jsonb`.
@dataclass(frozen=True)
class _StoredReceipt:
    request: object
    receipt: object


_SELECT = (
    "SELECT n.id, n.agent_id, t.label AS agent_label, n.title, n.content, n.priority, "
    "n.require_response, n.blocking, n.created_at, n.updated_at, "
    "n.resolved_at, n.resolution, n.reply, n.task_id, n.expire_at, n.question, n.answer, "
    "n.source_verified_by "
    "FROM agent_notices n JOIN agents t ON t.id = n.agent_id "
)

# meiva:note
# `_ACTION_RESOLUTION` maps an action to its stored resolution explicitly, never
# inferred from reply presence. The caps bound one response's payload; the
# configurable surface is the *default* window (user ruling 2026-09-17 / #3696).
# meiva:link gateway/routers/docs/frontend-ui-data.md
_ACTION_RESOLUTION = {"answer": "answered", "dismiss": "dismissed"}
_OPEN_FEED_MAX_LIMIT = 500


def _open_notices_blocking(
    pool: ConnectionPool, limit: int, *, agent_id: int | None = None
) -> list[NoticeItem]:
    # meiva:link gateway/routers/docs/frontend-ui-data.md
    with pool.connection() as conn, conn.cursor(row_factory=class_row(NoticeItem)) as cur:
        agent_clause = "" if agent_id is None else "AND n.agent_id = %s "
        params = (limit,) if agent_id is None else (agent_id, limit)
        cur.execute(
            _SELECT
            + "WHERE n.require_response AND n.resolved_at IS NULL "
            + agent_clause
            + "ORDER BY n.created_at ASC, n.id ASC LIMIT %s",
            params,
        )
        return cur.fetchall()


@router.get("/api/notices/open")
async def get_open_notices(
    request: Request,
    limit: Annotated[int | None, Query(ge=1, le=_OPEN_FEED_MAX_LIMIT)] = None,
    agent_id: Annotated[int | None, Query(ge=1)] = None,
) -> list[NoticeItem]:
    # meiva:link gateway/routers/docs/frontend-ui-data.md
    if limit is None:
        limit = gateway_config(request).display.notices_open_default_limit
    return await asyncio.to_thread(
        _open_notices_blocking,
        gateway_state(request).db_pool,
        limit,
        agent_id=agent_id,
    )


@router.get("/api/notices")
async def get_notices_feed(
    request: Request,
    limit: Annotated[int | None, Query(ge=1, le=_OPEN_FEED_MAX_LIMIT)] = None,
) -> NoticesFeed:
    # meiva:link gateway/routers/docs/frontend-ui-data.md
    if limit is None:
        limit = gateway_config(request).display.notices_open_default_limit
    awaiting = await asyncio.to_thread(
        _open_notices_blocking, gateway_state(request).db_pool, limit
    )
    return NoticesFeed(awaiting=awaiting)


async def post_notice_resolve(
    agent_id: int,
    notice_id: int,
    body: ResolveNoticeIn,
    request: Request,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=128),
) -> AgentMessageEnqueued:
    # meiva:note
    # Resolution and reply delivery share one durable receipt. A dismissal is a
    # notice-system event, never user authorization or ordinary user speech.
    # meiva:link agent/messages/docs/message-format.md
    idempotency_key = notice_key(request, idempotency_key)
    action = body.action
    reply = body.reply

    resolution: _ops.NoticeResolutionFacts | None = None

    def _resolve(conn: psycopg.Connection) -> str | None:
        nonlocal resolution
        if action == "read":
            raise HTTPException(
                status_code=422, detail="notices require a reply or an explicit dismissal"
            )
        with conn.cursor(row_factory=class_row(_ResolveTarget)) as cur:
            cur.execute(
                "SELECT title, require_response, resolved_at, question, expire_at, created_at "
                "FROM agent_notices WHERE id = %s AND agent_id = %s FOR UPDATE",
                (notice_id, agent_id),
            )
            target = cur.fetchone()
            if target is None:
                raise HTTPException(
                    status_code=409,
                    detail=f"notice {notice_id} does not exist for agent {agent_id}",
                )
            if target.question is not None and question_expired(
                target.expire_at, now=gateway_clock(request).now()
            ):
                raise HTTPException(status_code=409, detail="this question has expired")
            resolved_answer = resolution_answer(body, target.question)
            reply = resolved_answer.reply
            resolution = _ops.notice_resolution_facts(
                _ACTION_RESOLUTION[action],
                require_response=target.require_response,
                created_at=target.created_at,
                now=gateway_clock(request).now(),
                replied=reply is not None or resolved_answer.answer is not None,
            )
            if target.resolved_at is not None:
                raise HTTPException(
                    status_code=409,
                    detail=f"notice {notice_id} is already resolved for agent {agent_id}",
                )
            if not target.require_response:
                raise HTTPException(status_code=409, detail="legacy FYI entries are not notices")
            if action == "answer" and reply is None:
                raise HTTPException(status_code=422, detail="action 'answer' requires a reply")
            cur.execute(
                "UPDATE agent_notices SET resolved_at = now(), resolution = %s, reply = %s, answer = %s "
                "WHERE id = %s",
                (
                    _ACTION_RESOLUTION[action],
                    reply,
                    Jsonb(resolved_answer.answer.model_dump(mode="json"))
                    if resolved_answer.answer is not None
                    else None,
                    notice_id,
                ),
            )
        return resolution_inbound(target.title, action, resolved_answer.reply)

    # meiva:note
    # Every notice resolution rides the notice-system source (reply / dismiss); the claim node
    # wraps both as a system note, so no User-role message is consumed.
    # meiva:link agent/messages/docs/message-format.md
    deliver_source = (
        "system:notice-reply"
        if reply is not None or body.answer is not None
        else "system:notice-dismiss"
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
            raise TypeError("notice inbound receipt must be an integer")
        await asyncio.to_thread(
            publish_inbound_wake,
            gateway_state(request).db,
            gateway_state(request).bus,
            agent_id,
            str(inbound_id),
        )
        content = receipt["content"]
        if not isinstance(content, str):
            raise TypeError("notice inbound content must be a string")
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
    await _ops.publish_notice_resolved(
        gateway_state(request).bus, agent_id, notice_id, facts=resolution
    )
    return AgentMessageEnqueued(status=status, inbound_id=inbound_id)


# meiva:note
# ── keyed notice-operation receipts (owned by the notice transaction, never a response cache) ──


# meiva:note
# Use the same credential scope as other gateway keyed operations.
def notice_key(request: Request, key: str) -> str:
    try:
        return request_key(request, key, method="POST", path=request.url.path)
    except PrincipalScopeError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


# meiva:note
# Serialize a notice operation and reject reuse with changed inputs.
def existing_receipt(
    conn: psycopg.Connection,
    path: str,
    key: str,
    body: dict[str, object],
) -> dict[str, object] | None:
    with conn.cursor(row_factory=class_row(_StoredReceipt)) as cur:
        cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (path + ":" + key,))
        cur.execute(
            "SELECT request, receipt FROM notice_operation_receipts "
            "WHERE path = %s AND operation_key = %s",
            (path, key),
        )
        row = cur.fetchone()
    if row is None:
        return None
    if row.request != body:
        raise HTTPException(
            status_code=409, detail="idempotency key identifies a different notice operation"
        )
    if not is_object_dict(row.receipt):
        raise TypeError("stored notice operation receipt is not an object")
    return row.receipt


# meiva:note
# Persist an immutable result in the transaction containing its effects.
def save_receipt(
    conn: psycopg.Connection,
    path: str,
    key: str,
    body: dict[str, object],
    receipt: dict[str, object],
) -> None:
    conn.execute(
        "INSERT INTO notice_operation_receipts (path, operation_key, request, receipt) "
        "VALUES (%s, %s, %s, %s)",
        (path, key, Jsonb(body), Jsonb(receipt)),
    )


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

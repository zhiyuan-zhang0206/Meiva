# meiva:note
# One submission settles every open question of one agent: each is answered or skipped, in one
# transaction, with one inbound to the agent and one receipt. The submission names the open set the
# user saw; when the set changed in between, nothing is settled and the latest set comes back.
# meiva:link meiva_builtins/plugins/meiva_fleet/docs/questions.md

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass

import psycopg
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from psycopg.rows import class_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from base.agents.inbox.insert import insert_inbound_message_in_transaction
from base.agents.inbox.wake import publish_inbound_wake
from base.agents.messages.inbound import InboundKind
from base.agents.messages.inbound_provenance import InboundProvenance
from base.api_contracts.ask import AnswerQuestions
from base.db.transaction import write_transaction
from base.events.notifications.publish import stage_open_questions
from gateway.agents.inbound_provenance import request_inbound_provenance
from gateway.agents.keyed_operations import existing_receipt, keyed_request_key, save_receipt
from gateway.agents.questions.answers import reply_block, result_answer
from gateway.agents.questions.feed import open_questions
from gateway.agents.schemas import AgentMessageEnqueued
from gateway.http.config import gateway_rpc, gateway_state
from ops import lifecycle as _ops


class OpenQuestionsChangedError(Exception):
    pass


@dataclass(frozen=True)
class _OpenRow:
    id: int
    title: str
    question: object


# meiva:note
# The agent's open questions are read under `FOR UPDATE` after the agent row lock the ask and close
# routes also take, so a submission, an ask and a close of one agent serialize.
def _settle(conn: psycopg.Connection, agent_id: int, body: AnswerQuestions) -> str:
    if conn.execute("SELECT id FROM agents WHERE id=%s FOR UPDATE", (agent_id,)).fetchone() is None:
        raise HTTPException(404, "agent does not exist")
    with conn.cursor(row_factory=class_row(_OpenRow)) as cur:
        cur.execute(
            "SELECT id, title, question FROM agent_questions "
            "WHERE agent_id=%s AND resolved_at IS NULL ORDER BY id FOR UPDATE",
            (agent_id,),
        )
        rows = cur.fetchall()
        if [row.id for row in rows] != sorted(body.expected_ids):
            raise OpenQuestionsChangedError
        results = {result.id: result for result in body.results}
        blocks: list[str] = []
        for row in rows:
            result = results[row.id]
            if result.skip:
                cur.execute(
                    "UPDATE agent_questions SET resolved_at = now(), resolution = 'dismissed' "
                    "WHERE id = %s",
                    (row.id,),
                )
                blocks.append(reply_block(row.id, row.title, None))
                continue
            settled = result_answer(result, row.question)
            cur.execute(
                "UPDATE agent_questions SET resolved_at = now(), resolution = 'answered', "
                "reply = %s, answer = %s WHERE id = %s",
                (settled.reply, Jsonb(settled.answer.model_dump(mode="json")), row.id),
            )
            blocks.append(reply_block(row.id, row.title, settled.reply))
    stage_open_questions(conn, agent_id, open_before=len(rows))
    return "\n\n".join(blocks)


async def post_answers(
    agent_id: int, body: AnswerQuestions, request: Request, idempotency_key: str
) -> AgentMessageEnqueued | JSONResponse:
    key = keyed_request_key(request, idempotency_key)
    deliver_source = (
        "system:question-reply"
        if any(not result.skip for result in body.results)
        else "system:question-dismiss"
    )
    state = gateway_state(request)
    try:
        receipt = await asyncio.to_thread(
            resolve_once,
            state.db_pool,
            request.url.path,
            key,
            body.model_dump(mode="json"),
            agent_id,
            deliver_source,
            lambda conn: _settle(conn, agent_id, body),
            request_inbound_provenance(request),
        )
    except OpenQuestionsChangedError:
        latest = await asyncio.to_thread(open_questions, state.db_pool, None, agent_id=agent_id)
        return JSONResponse(
            status_code=409,
            content={
                "detail": "the open questions changed; nothing was settled",
                "open": [item.model_dump(mode="json") for item in latest],
            },
        )
    inbound_id = receipt["inbound_id"]
    if not isinstance(inbound_id, int):
        raise TypeError("question inbound receipt must be an integer")
    await asyncio.to_thread(publish_inbound_wake, state.db, state.bus, agent_id, str(inbound_id))
    content = receipt["content"]
    if not isinstance(content, str):
        raise TypeError("question inbound content must be a string")
    await _ops.publish_inbound_arrived(
        state.bus, agent_id, inbound_id, "chat", deliver_source, content
    )
    status = await _ops.resurrect_if_terminated(
        state.db,
        state.bus,
        agent_id,
        trigger_inbound_id=inbound_id,
        trigger_inbound_kind=InboundKind.CHAT,
        local_machine=state.machine.name(),
        rpc=gateway_rpc(request),
    )
    for result in body.results:
        await _ops.publish_question_resolved(state.bus, agent_id, result.id)
    return AgentMessageEnqueued(status=status, inbound_id=inbound_id)


# meiva:note
# Settle and insert the reply with one durable operation receipt.
def resolve_once(
    pool: ConnectionPool,
    path: str,
    key: str,
    body: dict[str, object],
    agent_id: int,
    source: str,
    prepare: Callable[[psycopg.Connection], str],
    provenance: InboundProvenance,
) -> dict[str, object]:
    with write_transaction(pool) as conn:
        previous = existing_receipt(conn, path, key, body)
        if previous is not None:
            return previous
        content = prepare(conn)
        with conn.cursor() as cur:
            inbound_id, _ = insert_inbound_message_in_transaction(
                cur, agent_id, content, source, provenance=provenance
            )
        receipt: dict[str, object] = {"inbound_id": inbound_id, "content": content}
        save_receipt(conn, path, key, body, receipt)
    return receipt

# meiva:note
# Persistent questions reuse the existing answer store and transaction receipts. An agent can close
# its own unanswered question; it can never edit one.

import asyncio
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from psycopg.rows import class_row, scalar_row
from psycopg.types.json import Jsonb
from pydantic import BaseModel

from base.api_contracts.notice_question import ChoiceQuestion
from base.api_contracts.user_delivery import AskMessage
from base.db.transaction import write_transaction
from base.events.notifications.publish import stage_question_asked, stage_question_resolved
from gateway.agents.inbound_provenance import request_inbound_provenance
from gateway.agents.notice_operations.keyed_operation import operation_key
from gateway.agents.notice_operations.notices import existing_receipt, save_receipt
from gateway.http.config import gateway_state
from ops.lifecycle import publish_notice_posted, publish_notice_resolved

router = APIRouter()


class _CloseTarget(BaseModel):
    title: str
    resolution: str | None


@router.post("/api/agents/{agent_id}/asks", status_code=201)
async def ask(
    agent_id: int,
    body: AskMessage,
    request: Request,
    key: Annotated[str, Depends(operation_key)],
) -> dict[str, int]:
    if isinstance(body.question, ChoiceQuestion) and body.question.allow_other:
        raise HTTPException(422, "Every question has a Note; Other is not an option")
    verified_by = request_inbound_provenance(request).source_verified_by
    payload = body.model_dump(mode="json")

    def write() -> int:
        with write_transaction(gateway_state(request).db_pool) as conn:
            receipt = existing_receipt(conn, request.url.path, key, payload)
            if receipt is not None:
                value = receipt["id"]
                if not isinstance(value, int):
                    raise TypeError("question receipt must contain an integer id")
                return value
            with conn.cursor(row_factory=scalar_row) as cur:
                cur.execute("SELECT id FROM agents WHERE id=%s FOR UPDATE", (agent_id,))
                if cur.fetchone() is None:
                    raise HTTPException(404, "agent does not exist")
                cur.execute(
                    "INSERT INTO agent_notices (agent_id, title, content, question, source_verified_by) "
                    "VALUES (%s,%s,%s,%s,%s) RETURNING id",
                    (
                        agent_id,
                        body.title,
                        body.content,
                        Jsonb(payload["question"]),
                        verified_by,
                    ),
                )
                question_id = cur.fetchone()
                if not isinstance(question_id, int):
                    raise TypeError("question insert must return an integer id")
            stage_question_asked(conn, agent_id=agent_id, notice_id=question_id, title=body.title)
            save_receipt(conn, request.url.path, key, payload, {"id": question_id})
            return question_id

    question_id = await asyncio.to_thread(write)
    await publish_notice_posted(gateway_state(request).bus, agent_id, question_id, body.title)
    return {"id": question_id}


# meiva:note
# The agent closes its own open question: `resolution='withdrawn'`, the question's push is retracted
# exactly as an answer retracts it, and no inbound is delivered (the agent did this itself). A question
# of another agent and one that does not exist are the same 404, so the route names no one else's
# question. A question the user answered or dismissed is a 409 that names the outcome; one already
# withdrawn is a no-op success.
@router.post("/api/agents/{agent_id}/asks/{question_id}/close")
async def close_ask(
    agent_id: int,
    question_id: int,
    request: Request,
    key: Annotated[str, Depends(operation_key)],
) -> dict[str, int]:
    payload: dict[str, object] = {"question_id": question_id}

    def write() -> bool:
        with write_transaction(gateway_state(request).db_pool) as conn:
            if existing_receipt(conn, request.url.path, key, payload) is not None:
                return False
            with conn.cursor(row_factory=class_row(_CloseTarget)) as cur:
                cur.execute(
                    "SELECT title, resolution FROM agent_notices "
                    "WHERE id=%s AND agent_id=%s FOR UPDATE",
                    (question_id, agent_id),
                )
                target = cur.fetchone()
                if target is None:
                    raise HTTPException(404, f"question {question_id} does not exist")
                if target.resolution is not None and target.resolution != "withdrawn":
                    raise HTTPException(
                        409, f"question {question_id} was already {target.resolution}"
                    )
                closed = target.resolution is None
                if closed:
                    cur.execute(
                        "UPDATE agent_notices SET resolved_at=now(), resolution='withdrawn' "
                        "WHERE id=%s",
                        (question_id,),
                    )
            if closed:
                stage_question_resolved(
                    conn, agent_id=agent_id, notice_id=question_id, title=target.title
                )
            save_receipt(conn, request.url.path, key, payload, payload)
            return closed

    if await asyncio.to_thread(write):
        await publish_notice_resolved(gateway_state(request).bus, agent_id, question_id)
    return {"id": question_id}

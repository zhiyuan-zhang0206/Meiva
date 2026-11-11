# meiva:note
# Persistent questions reuse the existing answer store and transaction receipts. An agent can close
# its own unanswered question; it can never edit one.

import asyncio
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from psycopg.rows import class_row, scalar_row
from psycopg.types.json import Jsonb
from pydantic import BaseModel

from base.api_contracts.ask import AskBatch, AskResult
from base.api_contracts.question import ChoiceQuestion
from base.db.transaction import write_transaction
from base.events.notifications.publish import open_question_count, stage_open_questions
from gateway.agents.inbound_provenance import request_inbound_provenance
from gateway.agents.keyed_operations import existing_receipt, operation_key, save_receipt
from gateway.http.config import gateway_state
from ops.lifecycle import publish_question_asked, publish_question_resolved

router = APIRouter()


class _CloseTarget(BaseModel):
    title: str
    resolution: str | None


@router.post("/api/agents/{agent_id}/asks", status_code=201)
async def ask(
    agent_id: int,
    body: AskBatch,
    request: Request,
    key: Annotated[str, Depends(operation_key)],
) -> AskResult:
    for item in body.questions:
        if isinstance(item.question, ChoiceQuestion) and item.question.allow_other:
            raise HTTPException(422, "Every question has a Note; Other is not an option")
    verified_by = request_inbound_provenance(request).source_verified_by
    payload = body.model_dump(mode="json")

    def write() -> tuple[AskResult, bool]:
        with write_transaction(gateway_state(request).db_pool) as conn:
            receipt = existing_receipt(conn, request.url.path, key, payload)
            if receipt is not None:
                return AskResult.model_validate(receipt), False
            with conn.cursor(row_factory=scalar_row) as cur:
                cur.execute("SELECT id FROM agents WHERE id=%s FOR UPDATE", (agent_id,))
                if cur.fetchone() is None:
                    raise HTTPException(404, "agent does not exist")
            open_before = open_question_count(conn, agent_id)
            ids: list[int] = []
            with conn.cursor(row_factory=scalar_row) as cur:
                for item, raw in zip(body.questions, payload["questions"], strict=True):
                    cur.execute(
                        "INSERT INTO agent_questions "
                        "(agent_id, title, content, question, source_verified_by) "
                        "VALUES (%s,%s,%s,%s,%s) RETURNING id",
                        (agent_id, item.title, item.content, Jsonb(raw["question"]), verified_by),
                    )
                    question_id = cur.fetchone()
                    if not isinstance(question_id, int):
                        raise TypeError("question insert must return an integer id")
                    ids.append(question_id)
            stage_open_questions(conn, agent_id, open_before=open_before)
            result = AskResult(ids=ids)
            save_receipt(conn, request.url.path, key, payload, result.model_dump(mode="json"))
            return result, True

    result, created = await asyncio.to_thread(write)
    if created:
        for question_id, item in zip(result.ids, body.questions, strict=True):
            await publish_question_asked(
                gateway_state(request).bus, agent_id, question_id, item.title
            )
    return result


# meiva:note
# The agent closes its own open question: `resolution='withdrawn'`, the agent's question push is
# updated or retracted like any change of its open set, and no inbound is delivered (the agent did this itself). A question
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
            conn.execute("SELECT id FROM agents WHERE id=%s FOR UPDATE", (agent_id,))
            open_before = open_question_count(conn, agent_id)
            with conn.cursor(row_factory=class_row(_CloseTarget)) as cur:
                cur.execute(
                    "SELECT title, resolution FROM agent_questions "
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
                        "UPDATE agent_questions SET resolved_at=now(), resolution='withdrawn' "
                        "WHERE id=%s",
                        (question_id,),
                    )
            if closed:
                stage_open_questions(conn, agent_id, open_before=open_before)
            save_receipt(conn, request.url.path, key, payload, payload)
            return closed

    if await asyncio.to_thread(write):
        await publish_question_resolved(gateway_state(request).bus, agent_id, question_id)
    return {"id": question_id}

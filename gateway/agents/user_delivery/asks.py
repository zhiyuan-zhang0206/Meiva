# meiva:note
# Persistent questions reuse the existing answer store and transaction receipts.

import asyncio
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from psycopg.rows import scalar_row
from psycopg.types.json import Jsonb

from base.api_contracts.notice_question import ChoiceQuestion
from base.api_contracts.user_delivery import AskMessage
from base.db.transaction import write_transaction
from gateway.agents.inbound_provenance import request_inbound_provenance
from gateway.agents.notice_operations.current import operation_key
from gateway.agents.notice_operations.notices import existing_receipt, save_receipt
from gateway.http.config import gateway_state
from ops.lifecycle import publish_notice_posted

router = APIRouter()


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
                    "INSERT INTO agent_notices (agent_id, local_id, title, content, priority, "
                    "require_response, blocking, expire_at, question, source_verified_by) "
                    "VALUES (%s, COALESCE((SELECT MAX(local_id) FROM agent_notices WHERE agent_id=%s), -1)+1, "
                    "%s,%s,'P2',true,false,NULL,%s,%s) RETURNING id",
                    (
                        agent_id,
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
            save_receipt(conn, request.url.path, key, payload, {"id": question_id})
            return question_id

    question_id = await asyncio.to_thread(write)
    await publish_notice_posted(
        gateway_state(request).bus, agent_id, question_id, "P2", body.title, None
    )
    return {"id": question_id}

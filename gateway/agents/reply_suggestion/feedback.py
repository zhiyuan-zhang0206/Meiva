# meiva:note
# Explicit suggestion feedback is stored separately from Agent messages and drafts.

import asyncio
from enum import StrEnum
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from psycopg import Connection
from psycopg.rows import dict_row, scalar_row
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

from base.db.transaction import write_transaction
from gateway.agents.notice_operations.current import operation_key
from gateway.agents.notice_operations.notices import existing_receipt, save_receipt
from gateway.http.config import gateway_state

router = APIRouter()


class FeedbackOutcome(StrEnum):
    ACCEPTED = "accepted"
    EDITED = "edited"
    REJECTED = "rejected"


class SuggestionFeedback(BaseModel):
    model_config = ConfigDict(extra="forbid")
    offer_id: int = Field(gt=0)
    candidate_index: int = Field(ge=0)
    outcome: FeedbackOutcome
    sent_text: str | None = Field(default=None, max_length=100_000)

    @model_validator(mode="after")
    def check_sent_text(self) -> "SuggestionFeedback":
        if self.outcome is FeedbackOutcome.REJECTED:
            if self.sent_text is not None:
                raise ValueError("rejection carries no sent text")
        elif self.sent_text is None or not self.sent_text.strip():
            raise ValueError("acceptance or edit requires the sent text")
        return self


def save_offer(conn: Connection, agent_id: int, context: str, suggestions: list[str]) -> int:
    with conn.cursor(row_factory=scalar_row) as cur:
        cur.execute(
            "INSERT INTO reply_suggestion_offers (agent_id,context,suggestions) VALUES (%s,%s,%s) RETURNING id",
            (agent_id, context[:12000], Jsonb(suggestions)),
        )
        value = cur.fetchone()
    if not isinstance(value, int):
        raise TypeError("suggestion offer must return an id")
    return value


@router.post("/api/agents/{agent_id}/reply-suggestion/feedback")
async def record_feedback(
    agent_id: int,
    body: SuggestionFeedback,
    request: Request,
    key: Annotated[str, Depends(operation_key)],
) -> dict[str, bool]:
    payload = body.model_dump(mode="json")

    def write() -> None:
        with write_transaction(gateway_state(request).db_pool) as conn:
            if existing_receipt(conn, request.url.path, key, payload) is not None:
                return
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    "SELECT suggestions FROM reply_suggestion_offers WHERE id=%s AND agent_id=%s",
                    (body.offer_id, agent_id),
                )
                row = cur.fetchone()
                if row is None:
                    raise HTTPException(404, "suggestion offer does not exist")
                candidates = TypeAdapter(list[str]).validate_python(row["suggestions"])
                if body.candidate_index >= len(candidates):
                    raise HTTPException(422, "candidate index is outside this offer")
                candidate = candidates[body.candidate_index]
                same = candidate == body.sent_text
                if (body.outcome is FeedbackOutcome.ACCEPTED and not same) or (
                    body.outcome is FeedbackOutcome.EDITED and same
                ):
                    raise HTTPException(422, "feedback outcome does not match the sent text")
                cur.execute(
                    "INSERT INTO reply_suggestion_feedback (offer_id,candidate,outcome,sent_text) VALUES (%s,%s,%s,%s)",
                    (body.offer_id, candidate, body.outcome.value, body.sent_text),
                )
            save_receipt(conn, request.url.path, key, payload, {"saved": True})

    await asyncio.to_thread(write)
    return {"saved": True}

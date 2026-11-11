# meiva:note
# Keyed question resolution requiring observed identity and transactional receipts.

from typing import Annotated

from fastapi import APIRouter, Depends, Header, Request

from gateway.agents.keyed_operations import operation_key
from gateway.agents.questions.resolve import post_question_resolve
from gateway.agents.schemas import AgentMessageEnqueued, ResolveQuestionIn

router = APIRouter()


# meiva:note
# Resolve the explicit global row using verified transactional recovery.
@router.post(
    "/api/keyed/v1/agents/{agent_id}/questions/{question_id}/resolve",
    status_code=201,
    dependencies=[Depends(operation_key)],
)
async def resolve_observed(
    agent_id: int,
    question_id: int,
    body: ResolveQuestionIn,
    request: Request,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=128)],
) -> AgentMessageEnqueued:
    # meiva:note
    # Admission validates principal scope; the existing owner scopes the raw
    # key against this concrete path and commits the resolution plus receipt.
    return await post_question_resolve(agent_id, question_id, body, request, idempotency_key)

# meiva:note
# Keyed question answering requiring observed identity and transactional receipts.

from typing import Annotated

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse

from base.api_contracts.ask import AnswerQuestions
from gateway.agents.keyed_operations import operation_key
from gateway.agents.questions.resolve import post_answers
from gateway.agents.schemas import AgentMessageEnqueued

router = APIRouter()


@router.post(
    "/api/keyed/v1/agents/{agent_id}/questions/answers",
    status_code=201,
    dependencies=[Depends(operation_key)],
    response_model=AgentMessageEnqueued,
    responses={
        409: {"description": "The open questions changed; the body carries the latest set."}
    },
)
async def answer_questions(
    agent_id: int,
    body: AnswerQuestions,
    request: Request,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=128)],
) -> AgentMessageEnqueued | JSONResponse:
    return await post_answers(agent_id, body, request, idempotency_key)

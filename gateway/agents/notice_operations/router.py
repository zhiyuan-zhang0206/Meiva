# meiva:note
# Keyed notice resolution requiring observed identity and transactional receipts.

from typing import Annotated

from fastapi import APIRouter, Depends, Header, Request

from gateway.agents.notice_operations.keyed_operation import operation_key
from gateway.agents.notice_operations.notices import post_notice_resolve
from gateway.agents.schemas import AgentMessageEnqueued, ResolveNoticeIn

router = APIRouter()


@router.post(
    "/api/keyed/v1/agents/{agent_id}/notices/{notice_id}/resolve",
    status_code=201,
    dependencies=[Depends(operation_key)],
)
async def resolve_observed(
    agent_id: int,
    notice_id: int,
    body: ResolveNoticeIn,
    request: Request,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=128)],
) -> AgentMessageEnqueued:
    """Resolve the explicit global row using verified transactional recovery."""
    # Admission validates principal scope; the existing owner scopes the raw
    # key against this concrete path and commits the resolution plus receipt.
    return await post_notice_resolve(agent_id, notice_id, body, request, idempotency_key)

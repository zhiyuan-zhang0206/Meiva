import asyncio

from fastapi import HTTPException, Request
from pydantic import BaseModel

from base.api_contracts.agent_contract import AgentStatus
from base.db.rows import query_one
from gateway.http.config import gateway_state
from ops.rpc_schemas import HarnessTimelineWindow


class _StatusRow(BaseModel):
    status: AgentStatus


def _status_of(request: Request, agent_id: int) -> AgentStatus:
    with gateway_state(request).db_pool.connection() as conn:
        row = query_one(
            conn, _StatusRow, "SELECT status FROM agents_meta WHERE id = %s", (agent_id,)
        )
    if row is None:
        raise HTTPException(404, "Agent not found")
    return row.status


# meiva:note
# Call after awaiting the record RPC. Activity reflects canonical status at completion of the read;
# record completeness and exact source identity remain the reader and admission boundaries.
async def settle_record_activity(
    request: Request, agent_id: int, window: HarnessTimelineWindow
) -> HarnessTimelineWindow:
    status = await asyncio.to_thread(_status_of, request, agent_id)
    if status is not AgentStatus.RUNNING:
        items = [
            item.model_copy(update={"activity_pending": None}) if item.activity_pending else item
            for item in window.items
        ]
        return window.model_copy(update={"items": items})
    return window

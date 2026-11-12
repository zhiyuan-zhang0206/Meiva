from __future__ import annotations

import asyncio
import logging

import anyio.from_thread
from fastapi import HTTPException, Request
from pydantic import BaseModel

from base.agents.history.timeline_item import TimelineItem
from base.agents.to_user.delivery.store import delivery_mode
from base.api_contracts.agent_contract import reads_timeline_from_record
from base.api_contracts.user_delivery import UserDeliveryMode
from base.db.rows import query_one
from gateway.agents.history.configuration import request_timeline_read
from gateway.agents.history.cursor import TimelineCursor
from gateway.agents.state import agent_harness
from gateway.http.config import gateway_config, gateway_rpc, gateway_state
from ops.cluster.rpc import ClusterOpFailed, ClusterOpUnreachable
from ops.rpc_schemas import HarnessTimelineReadRequest, HarnessTimelineWindow

_log = logging.getLogger(__name__)

# meiva:note
# The gateway's side of a timeline that is read from the agent's CLI record: one `harness_timeline_read` operation to
# the machine the agent lives on, with a short timeout and no retry (a timeline read is asked again by the page).
# A machine that cannot be reached is a 503, as for any read of the agent's machine: the record is on that machine and
# nowhere else, so its history is not shown while it is offline. A failure of the operation itself is a 502. The
# sync routes ask through the event loop that serves them, and the reply-suggestion route (async) asks directly.
# meiva:link docs/decisions/agents/graph/2026-11-13-timeline-read-from-the-record.md

READ_TIMEOUT_SECONDS = 3.0


class _MachineRow(BaseModel):
    machine: str


def _placement(request: Request, agent_id: int) -> tuple[str, UserDeliveryMode]:
    with gateway_state(request).db_pool.connection() as conn:
        row = query_one(
            conn, _MachineRow, "SELECT machine FROM agents_meta WHERE id = %s", (agent_id,)
        )
        if row is None:
            raise HTTPException(404, "Agent not found")
        mode = delivery_mode(conn, agent_id, gateway_config(request).user_delivery_mode)
    return row.machine, mode


async def fetch_record_window(
    request: Request, agent_id: int, harness: str, *, limit: int, before: int | None
) -> HarnessTimelineWindow:
    state = gateway_state(request)
    machine, mode = await asyncio.to_thread(_placement, request, agent_id)
    payload = HarnessTimelineReadRequest(
        agent_id=agent_id,
        harness=harness,
        limit=limit,
        before=before,
        explicit_visibility=mode.value == "prefix",
        delivery_mode=mode.value,
    )
    try:
        result = await gateway_rpc(request).dispatch_to_machine(
            state.db,
            target_machine=machine,
            kind="harness_timeline_read",
            payload=payload.model_dump(),
            timeout_s=READ_TIMEOUT_SECONDS,
            retries=0,
        )
    except ClusterOpUnreachable as exc:
        raise HTTPException(503, "The agent's machine is unavailable") from exc
    except ClusterOpFailed as exc:
        _log.warning("harness_timeline_read failed for agent %s: %s", agent_id, exc.result)
        raise HTTPException(502, "The agent's machine could not read the timeline") from exc
    return HarnessTimelineWindow.model_validate(result)


def fetch_record_window_sync(
    request: Request, agent_id: int, harness: str, *, limit: int, before: int | None
) -> HarnessTimelineWindow:
    async def ask() -> HarnessTimelineWindow:
        return await fetch_record_window(request, agent_id, harness, limit=limit, before=before)

    return anyio.from_thread.run(ask)


# meiva:note
# The window of an agent whose timeline is read from its CLI's record: the items, the record's size (the exclusive bound
# of their offsets, the timeline's `msg_count`) and whether older items exist. A page `before` an item asks for the window
# ending at that item's row; a cursor of another kind has nothing to ask the record.
def record_window_for(
    request: Request,
    agent_id: int,
    harness: str,
    limit: int,
    before: str | None,
    cursor: TimelineCursor | None,
) -> tuple[list[TimelineItem], int, bool]:
    if before is not None and (cursor is None or not cursor.is_harness or cursor.checkpoint_id):
        return [], 0, False
    offset = None if before is None or cursor is None else cursor.msg_idx
    window = fetch_record_window_sync(request, agent_id, harness, limit=limit, before=offset)
    return window.items, window.size, window.has_more


def _harness_of(request: Request, agent_id: int) -> str:
    with gateway_state(request).db_pool.connection() as conn:
        return agent_harness(conn, agent_id)


# meiva:note
# The newest window of an agent whose timeline is read from its record, or nothing for any other agent.
async def record_items_of(request: Request, agent_id: int) -> list[TimelineItem]:
    harness = await asyncio.to_thread(_harness_of, request, agent_id)
    if not reads_timeline_from_record(harness):
        return []
    limit = request_timeline_read(request).default_limit
    return (await fetch_record_window(request, agent_id, harness, limit=limit, before=None)).items

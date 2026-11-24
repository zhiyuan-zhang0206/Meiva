"""Agent live-event endpoint — `GET /api/agents/{id}/events/stream`.

The live SSE tail: subscribe to Redis `meiva:events`, filter by `agent_id`. It only
carries events from the moment you subscribe; history lives in Loki.

Split out of the agents CRUD router (CRUD + lifecycle then; lifecycle later
moved to `gateway/agents/lifecycle.py`) so each router stays a
focused unit — same precedent as gateway/inspect/router.py (the per-agent
inspector panel).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from gateway.agents.eval_guard import deny_isolated_result_read
from gateway.events.sse import event_stream
from gateway.events.sse_frames import SseCadence
from gateway.http.config import gateway_config, gateway_state

router = APIRouter()


@router.get(
    "/api/agents/{agent_id}/events/stream",
    dependencies=[Depends(deny_isolated_result_read)],
)
async def get_events_stream(agent_id: int, request: Request) -> StreamingResponse:
    """SSE endpoint — subscribe to Redis `meiva:events`, filter by `agent_id`,
    forward.

    Client `EventSource` receives `data: {json}\\n\\n` frames, one JSON
    `events.Event` per frame. code_delta chunks pass through immediately,
    native streaming. This is the live tail only; there is no historical
    REST query per agent (`GET /api/events?agent_id=` is the history surface).

    Does **not** check agent_exists as a precondition: subscribing to a
    non-existent agent is allowed, you just receive no messages. Otherwise
    agents that were just switched / newly created but with slight DB
    visibility lag would 404.
    """
    return StreamingResponse(
        event_stream(
            gateway_state(request).bus,
            agent_id,
            request,
            poll_s=gateway_config(request).sse.disconnect_poll_seconds,
            cadence=SseCadence(),
        ),
        media_type="text/event-stream",
        headers={
            # Reverse proxies like nginx / cloudflare buffer text responses
            # by default — these two headers tell them to pass bytes through.
            # Not needed when running uvicorn directly, but without them
            # everything breaks behind a proxy; cheap to write so we do it.
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )

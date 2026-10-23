# meiva:note
# System broadcast SSE endpoints — /api/system, /api/agents/{agent_id}/system,
# /api/system/all.
#
# Three channels:
# - /api/system forwards GLOBAL_ROLES for *all* agents (the cross-agent,
#   low-frequency lifecycle the sidebar / pages popover need).
# - /api/agents/{id}/system forwards the full SYSTEM_ROLES for one agent (the
#   high-frequency per-turn stream the timeline of the observed agent needs).
# - /api/system/all forwards a throttled, batched stream (push cadence set by
#   gateway.sse_throttle_rate, each push a JSON array of events).
#   An optional agents query filters by agent_id while retaining system-level
#   agent_id=0 events; there is no role filtering.

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from base.events.live.projection import GLOBAL_ROLES, SYSTEM_ROLES
from gateway.events.merged import parse_agents
from gateway.events.sse import event_stream, throttled_event_stream
from gateway.events.sse_frames import SseCadence
from gateway.http.config import runtime_view

router = APIRouter()


@router.get("/api/system")
async def get_system_broadcast(request: Request) -> StreamingResponse:
    """Global SSE broadcast — does not filter by agent_id, forwards the
    cross-agent low-frequency GLOBAL_ROLES (agent lifecycle + page open/close)
    for every agent.

    The per-turn streaming roles are NOT here — clients subscribe to
    /api/agents/{id}/system for the agent they are observing.
    """
    return StreamingResponse(
        event_stream(
            request.app.state.bus,
            0,  # agent_id is ignored in broadcast mode
            request,
            role_filter=GLOBAL_ROLES,
            broadcast=True,
            poll_s=runtime_view(request).gateway.sse_disconnect_poll_seconds,
            cadence=SseCadence(),
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/api/agents/{agent_id}/system")
async def get_system_events(agent_id: int, request: Request) -> StreamingResponse:
    """SSE endpoint — subscribe to `meiva:events`, only forward SYSTEM_ROLES events.

    SYSTEM_ROLES contains code_delta / exec_output / compact_done /
    cancelled etc. "system state" events — clients that need to track the
    kernel runtime state subscribe to this channel.
    """
    return StreamingResponse(
        event_stream(
            request.app.state.bus,
            agent_id,
            request,
            role_filter=SYSTEM_ROLES,
            poll_s=runtime_view(request).gateway.sse_disconnect_poll_seconds,
            cadence=SseCadence(),
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.get(
    "/api/system/all",
    description="Throttled SSE stream with optional comma-separated agent filtering.\n\n    With no ``agents`` query, every event for every agent passes. With a query,\n    only the selected agents plus system-level ``agent_id == 0`` events pass.\n    There is no role filtering. Events are batched and flushed at the cadence\n    set by gateway.sse_throttle_rate, each flush carrying a JSON array\n    of raw event payloads.\n\n    Wire format: `data: [event_json, ...]\n\n`\n\n    The frontend subscribes once for the active agent and still filters by\n    agent_id / role defensively.\n    ",
)
async def get_all_events(request: Request, agents: str | None = None) -> StreamingResponse:
    agent_filter = parse_agents(agents)
    return StreamingResponse(
        throttled_event_stream(
            request.app.state.bus,
            request,
            throttle_rate=runtime_view(request).gateway.sse_throttle_rate,
            cadence=SseCadence(),
            agent_filter=agent_filter,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )

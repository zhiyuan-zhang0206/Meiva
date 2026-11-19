"""Claim-node display logic: SSE event publishing for the frontend timeline.

Extracted from agent/graph/claim/node.py (Phase 2 display isolation).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from langchain_core.messages import BaseMessage

from agent.graph.node_log import SNAPSHOT_CURSOR
from agent.state import BaseAgentState
from base.agents.context import MeivaContext
from base.agents.history.timeline import build_timeline_items, tail_window
from base.agents.history.timeline_item import TurnTimingContext
from base.events.live.projection import InboundCommitted, TimelineSnapshot, TurnStarted


def publish_turn_started(
    ctx: MeivaContext,
    agent_id: int,
    *,
    turn_id: str,
    started_at: str,
) -> None:
    if ctx.event_publisher is None:
        return
    ctx.event_publisher.emit(
        TurnStarted(
            agent_id=agent_id,
            turn_id=turn_id,
            started_at=started_at,
        ).model_dump_json()
    )


def publish_turn_closed_timeline_snapshot(
    ctx: MeivaContext,
    state: BaseAgentState,
    agent_id: int,
    *,
    turn_timings: Sequence[Mapping[str, object]],
    start_idx: int,
) -> None:
    if ctx.event_publisher is None or not state.messages or start_idx >= len(state.messages):
        return
    delta, msg_count = build_timeline_items(
        state.messages,
        start=start_idx,
        inputs=ctx.timestamp_policy(),
        timing=TurnTimingContext(timings=turn_timings),
    )
    if not delta:
        return
    SNAPSHOT_CURSOR[agent_id] = len(state.messages)
    ctx.event_publisher.emit(
        TimelineSnapshot(
            agent_id=agent_id,
            items=[it.model_dump() for it in delta],
            msg_count=msg_count,
            active_turn_id=None,
            active_turn_started_at=None,
        ).model_dump_json()
    )


# meiva:note
# Publish an incremental TimelineSnapshot for messages claimed in this batch before
# InboundCommitted fires, so the frontend renders the newly committed user message
# in the timeline before removing it from PendingStrip.
def publish_claimed_timeline_snapshot(
    ctx: MeivaContext,
    state: BaseAgentState,
    agent_id: int,
    new_msgs: list[BaseMessage],
    *,
    turn_timings: Sequence[Mapping[str, object]] | None = None,
    active_turn_id: str | None = None,
    active_turn_started_at: str | None = None,
    active_turn_start_idx: int | None = None,
) -> None:
    if ctx.event_publisher is None:
        raise RuntimeError("publish_claimed_timeline_snapshot requires ctx.event_publisher")
    if not new_msgs:
        return
    base_len = len(state.messages)
    combined = list(state.messages) + new_msgs
    delta, msg_count = build_timeline_items(
        combined,
        start=base_len,
        inputs=ctx.timestamp_policy(),
        timing=TurnTimingContext(
            timings=state.turn_timings if turn_timings is None else turn_timings,
            active_turn_id=active_turn_id,
            active_turn_start_idx=active_turn_start_idx,
        ),
    )
    if not delta:
        return
    SNAPSHOT_CURSOR[agent_id] = len(combined)
    ctx.event_publisher.emit(
        TimelineSnapshot(
            agent_id=agent_id,
            items=[it.model_dump() for it in delta],
            msg_count=msg_count,
            active_turn_id=active_turn_id,
            active_turn_started_at=active_turn_started_at,
        ).model_dump_json()
    )


async def publish_inbound_committed(
    ctx: MeivaContext, agent_id: int, inbound_ids: list[int]
) -> None:
    """Publish one InboundCommitted SSE event per chat inbound that has been
    envelope-wrapped into state.messages, triggering frontend timeline reload."""
    assert ctx.event_publisher is not None, (  # noqa: S101
        "publish_inbound_committed requires ctx.event_publisher"
    )
    for inbound_id in inbound_ids:
        ctx.event_publisher.emit(
            InboundCommitted(agent_id=agent_id, inbound_id=inbound_id).model_dump_json()
        )


def publish_end_timeline_snapshot(
    ctx: MeivaContext,
    state: BaseAgentState,
    agent_id: int,
    new_msgs: list[BaseMessage],
    *,
    turn_timings: Sequence[Mapping[str, object]] | None = None,
) -> None:
    """Publish a TimelineSnapshot when claim routes the agent to END.

    When claim routes the agent to END (terminate / restart), no further node
    enters, so the enter-time TimelineSnapshot (published by node_lifecycle
    before dispatch) missed the lifecycle markers appended here (e.g. the
    "You are terminated" system note). Without this final snapshot the
    frontend sees the markers only after the next manual refresh — publish
    the post-dispatch state now so the frontend renders them immediately.

    ctx.event_publisher is already narrowed to non-None by the container-mode
    early-return at the top of claim_node_impl.
    """
    assert ctx.event_publisher is not None, (  # noqa: S101
        "publish_end_timeline_snapshot requires ctx.event_publisher"
    )
    combined = list(state.messages) + new_msgs
    items, msg_count = build_timeline_items(
        combined,
        inputs=ctx.timestamp_policy(),
        timing=TurnTimingContext(
            timings=state.turn_timings if turn_timings is None else turn_timings,
        ),
    )
    window, _ = tail_window(items, ctx.require_agent().read("display", "timeline_default_limit"))
    # No system-prompt special-casing: this end-of-life snapshot is a rare
    # full-window publish, and 0.0 flows through the merge like any item.
    ctx.event_publisher.emit(
        TimelineSnapshot(
            agent_id=agent_id,
            items=[it.model_dump() for it in window],
            msg_count=msg_count,
        ).model_dump_json()
    )

# meiva:note
# Quote admission checks an exact readable source, without matching rendered text to Markdown.

import asyncio

from fastapi import HTTPException, Request
from pydantic import BaseModel

from base.agents.history.checkpoint import (
    CheckpointReadError,
    list_compact_boundary_checkpoint_ids,
    load_checkpoint_messages,
    load_checkpoint_messages_segment,
)
from base.agents.history.timeline import build_timeline_items
from base.agents.history.timeline_item import TimelineItem
from base.agents.messages.quote import Quote
from base.agents.messages.timestamp_policy import TimestampPolicy
from base.agents.to_user.delivery.store import delivery_mode
from base.api_contracts.user_delivery import UserDeliveryMode
from base.db.rows import query_one
from gateway.http.config import gateway_clock, gateway_config, gateway_rpc, gateway_state
from gateway.message_admission.cursor import TimelineCursor, parse_cursor
from gateway.message_admission.harness import agent_harness
from gateway.record_activity import settle_record_activity
from ops.cluster.rpc import ClusterOpFailed, ClusterOpUnreachable
from ops.rpc_schemas import HarnessTimelineReadRequest, HarnessTimelineWindow


class _Placement(BaseModel):
    machine: str


def _admission_context(request: Request, agent_id: int) -> tuple[str, str, UserDeliveryMode]:
    with gateway_state(request).db_pool.connection() as conn:
        harness = agent_harness(conn, agent_id)
        placement = query_one(
            conn, _Placement, "SELECT machine FROM agents_meta WHERE id = %s", (agent_id,)
        )
        mode = delivery_mode(conn, agent_id, gateway_config(request).user_delivery_mode)
    if placement is None:
        raise HTTPException(404, "Agent not found")
    return harness, placement.machine, mode


def _native_items(request: Request, agent_id: int, cursor: TimelineCursor) -> list[TimelineItem]:
    state, config = gateway_state(request), gateway_config(request)
    prefix = ""
    if cursor.checkpoint_id is None:
        messages = load_checkpoint_messages(state.db, agent_id)
    else:
        depth = config.timeline_compact_history
        if depth == 0:
            raise HTTPException(422, "Retained quote history is disabled")
        boundaries = list_compact_boundary_checkpoint_ids(
            state.db, agent_id, limit=depth if depth > 0 else None
        )
        if cursor.checkpoint_id not in boundaries:
            raise HTTPException(422, "Retained quote source is no longer available")
        messages = load_checkpoint_messages_segment(state.db, agent_id, cursor.checkpoint_id)
        # meiva:note
        # Rank is presentation only. Retain the selected cursor's prefix while checking its exact checkpoint.
        rank = boundaries.index(cursor.checkpoint_id) + 1
        prefix = f"s{rank}.{cursor.checkpoint_id}"
    items, _ = build_timeline_items(
        messages,
        inputs=TimestampPolicy.from_clock(
            gateway_clock(request), timestamps_enabled=config.message_timestamps
        ),
        segment_prefix=prefix,
    )
    return items


async def _record_items(
    request: Request,
    agent_id: int,
    cursor: TimelineCursor,
    context: tuple[str, str, UserDeliveryMode],
) -> list[TimelineItem]:
    harness, machine, mode = context
    read = HarnessTimelineReadRequest(
        agent_id=agent_id,
        harness=harness,
        limit=2,
        anchor_item_id=cursor.item_id(),
        explicit_visibility=mode == UserDeliveryMode.PREFIX,
        delivery_mode=mode.value,
    )
    try:
        result = await gateway_rpc(request).dispatch_to_machine(
            gateway_state(request).db,
            target_machine=machine,
            kind="harness_timeline_read",
            payload=read.wire_payload(),
            timeout_s=gateway_config(request).timeline_read_timeout_seconds,
            retries=0,
        )
    except ClusterOpUnreachable as exc:
        raise HTTPException(503, "Quote source machine is unavailable") from exc
    except ClusterOpFailed as exc:
        raise HTTPException(502, "Quote source timeline could not be read") from exc
    window = HarnessTimelineWindow.model_validate(result)
    if not window.anchor_applied:
        raise HTTPException(503, "Quote source reader cannot confirm an inclusive lookup")
    if window.anchor_item_id != cursor.item_id():
        raise HTTPException(
            422, "Quote source is unavailable; select readable, completed text again"
        )
    return (await settle_record_activity(request, agent_id, window)).items


def _check_identity(quote: Quote, source: TimelineItem) -> None:
    if source.kind == "system_marker" and not (
        source.source_message_id or source.source_inbound_id
    ):
        raise HTTPException(
            422, "Quote source has no stable message identity; select another source"
        )
    expected = (quote.source_message_id, quote.source_inbound_id, quote.source_block_idx)
    actual = (
        source.source_message_id,
        source.source_inbound_id or source.inbound_id,
        source.source_block_idx,
    )
    if any(
        value is not None and value != stored
        for value, stored in zip(expected, actual, strict=True)
    ):
        raise HTTPException(422, "Quote source identity changed; select it again")


def _source_item(quote: Quote, items: list[TimelineItem], cursor: TimelineCursor) -> TimelineItem:
    # meiva:note
    # A retained cursor's display rank may change after another compaction; its checkpoint stays authoritative.
    suffix = (
        f".{cursor.checkpoint_id}.{cursor.msg_idx}.{cursor.block_idx}"
        if cursor.checkpoint_id
        else None
    )
    source = next(
        (
            item
            for item in items
            if item.item_id == quote.source_item_id
            or (suffix is not None and item.item_id.endswith(suffix))
        ),
        None,
    )
    if (
        source is None
        or source.activity_pending
        or source.kind
        not in {
            "inbound_chat",
            "agent_chat",
            "agent_code",
            "agent_reasoning",
            "code_output",
            "system_marker",
        }
    ):
        raise HTTPException(
            422, "Quote source is unavailable; select readable, completed text again"
        )
    return source


async def validate_quote_source(request: Request, agent_id: int, quote: Quote | None) -> None:
    if quote is None:
        return
    if quote.source_agent_id != agent_id:
        raise HTTPException(422, "Quotes must come from the current conversation")
    cursor = parse_cursor(quote.source_item_id)
    if cursor is None:
        raise HTTPException(422, "This quote source is no longer locatable; select it again")
    if cursor.epoch is not None:
        raise HTTPException(422, "Quotes from an earlier harness session are not supported")
    context = await asyncio.to_thread(_admission_context, request, agent_id)
    if (context[0] != "meiva") != cursor.is_harness:
        raise HTTPException(422, "Quote source does not belong to this timeline")
    if cursor.is_harness:
        items = await _record_items(request, agent_id, cursor, context)
    else:
        try:
            items = await asyncio.to_thread(_native_items, request, agent_id, cursor)
        except CheckpointReadError as exc:
            raise HTTPException(503, "Quote source history is unavailable") from exc
    _check_identity(quote, _source_item(quote, items, cursor))

from __future__ import annotations

from base.agents.history.checkpoint import CheckpointTimelineSource, load_checkpoint_timeline_source
from base.agents.history.timeline import build_timeline_items, reading_target, reading_window
from base.agents.history.timeline_item import ReadingWindow, TimelineItem, TurnTimingContext
from base.agents.messages.timestamp_policy import TimestampPolicy
from base.api_contracts.reading import (
    NativeReadingLocator,
    NativeReadingSource,
    ReadingDirection,
    ReadingSourceUnavailableError,
    ReadingUnavailableReason,
    encode_reading_locator,
    encode_reading_source,
)
from base.db import Database


def native_source_identity(source: CheckpointTimelineSource, agent_id: int) -> NativeReadingSource:
    return NativeReadingSource(
        agent_id=agent_id, epoch=source.epoch, checkpoint_id=source.checkpoint_id
    )


def native_source_items(
    source: CheckpointTimelineSource,
    agent_id: int,
    rendering: TimestampPolicy,
    *,
    segment_prefix: str | None = None,
) -> list[TimelineItem]:
    if segment_prefix is None:
        segment_prefix = (
            ""
            if source.current
            else f"s{source.boundaries.index(source.checkpoint_id) + 1}.{source.checkpoint_id}"
        )
    items, _ = build_timeline_items(
        source.messages,
        inputs=rendering,
        segment_prefix=segment_prefix,
        timing=TurnTimingContext(
            timings=source.turn_timings,
            active_turn_id=source.active_turn_id,
            active_turn_start_idx=source.active_turn_start_idx,
        ),
    )
    identity = native_source_identity(source, agent_id)
    for item in items:
        if (
            item.kind == "system_prompt"
            or item.source_block_idx is None
            or not (item.source_message_id or item.source_inbound_id)
        ):
            continue
        locator = NativeReadingLocator(
            agent_id=identity.agent_id,
            epoch=identity.epoch,
            checkpoint_id=identity.checkpoint_id,
            message_id=item.source_message_id,
            inbound_id=item.source_inbound_id,
            block=item.source_block_idx,
        )
        item.reading_locator = encode_reading_locator(locator)
    return items


def _source_edges(source: CheckpointTimelineSource, depth: int) -> tuple[bool, bool]:
    rank = 0 if source.checkpoint_id is None else source.boundaries.index(source.checkpoint_id) + 1
    older = rank < len(source.boundaries) and (depth < 0 or rank < depth)
    return older, rank > 0


def _source_result(
    result: ReadingWindow, source: CheckpointTimelineSource, agent_id: int
) -> ReadingWindow:
    return result.model_copy(
        update={"reading_source": encode_reading_source(native_source_identity(source, agent_id))}
    )


def native_reading_window(
    db: Database,
    locator: NativeReadingLocator,
    direction: ReadingDirection,
    limit: int,
    depth: int,
    rendering: TimestampPolicy,
) -> ReadingWindow:
    source = load_checkpoint_timeline_source(db, locator.agent_id, depth, locator=locator)
    items = [
        item
        for item in native_source_items(source, locator.agent_id, rendering)
        if item.kind != "system_prompt"
    ]
    target = reading_target(items, locator)
    older, newer = _source_edges(source, depth)
    try:
        selected = reading_window(
            items, target, direction, limit, older_available=older, newer_available=newer
        )
        return _source_result(selected, source, locator.agent_id)
    except ReadingSourceUnavailableError as exc:
        if exc.reason is not ReadingUnavailableReason.SOURCE:
            raise
    identity = native_source_identity(source, locator.agent_id)
    del source, items
    return _adjacent_window(db, identity, direction, limit, depth, rendering)


def _adjacent_window(
    db: Database,
    identity: NativeReadingSource,
    direction: ReadingDirection,
    limit: int,
    depth: int,
    rendering: TimestampPolicy,
) -> ReadingWindow:
    source = load_checkpoint_timeline_source(
        db, identity.agent_id, depth, locator=identity, advance=direction
    )
    if native_source_identity(source, identity.agent_id) == identity:
        raise ReadingSourceUnavailableError(
            ReadingUnavailableReason.SOURCE, "The adjacent current source is not committed yet"
        )
    items = [
        item
        for item in native_source_items(source, identity.agent_id, rendering)
        if item.kind != "system_prompt"
    ]
    older, newer = _source_edges(source, depth)
    if not items:
        more = older if direction is ReadingDirection.BEFORE else newer
        if more:
            raise ReadingSourceUnavailableError(
                ReadingUnavailableReason.IDENTITY,
                "The intermediate source has no stable reading edge",
            )
        return _source_result(ReadingWindow(items=[]), source, identity.agent_id)
    target = len(items) - 1 if direction is ReadingDirection.BEFORE else 0
    result = reading_window(
        items, target, ReadingDirection.ANCHOR, limit, older_available=older, newer_available=newer
    )
    return _source_result(
        result.model_copy(update={"anchor_item_id": None}), source, identity.agent_id
    )

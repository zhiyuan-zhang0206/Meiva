"""Timeline endpoint — /api/agents/{agent_id}/timeline.

Cold-load path only (page mount / agent switch). During a turn the frontend
updates from agent-published `timeline_snapshot` events; this endpoint just
serves the initial full view. Both render through the same
`base.agents.history.timeline.build_timeline_items`, so the cold load and the live
snapshots agree item-for-item.

This is a cold-load checkpoint reader (see `base.agents.history.checkpoint` for the shared
read contract). Read failures return 503; retained compact history has a
separate explicit endpoint and never substitutes for the live checkpoint.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import replace

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from langchain_core.messages import BaseMessage
from pydantic import BaseModel, Field

from base.agents.epochs.model import HarnessEpoch
from base.agents.epochs.native_slice import native_slice_of
from base.agents.epochs.read import read_epochs
from base.agents.history.checkpoint import (
    CheckpointReadError,
    CheckpointTimelineSource,
    list_compact_boundary_checkpoint_ids,
    load_checkpoint_message_count,
    load_checkpoint_messages,
    load_checkpoint_timeline_source,
)
from base.agents.history.timeline import TimelineItem, build_timeline_items, tail_window
from base.agents.messages.timestamp_policy import TimestampPolicy
from base.api_contracts.agent_contract import reads_timeline_from_record
from base.api_contracts.reading import ReadingSourceUnavailableError, encode_reading_source
from base.db import Database, agent_exists
from base.log import logger
from gateway.agents.eval_guard import deny_isolated_result_read
from gateway.agents.history.configuration import request_timeline_read
from gateway.agents.history.epoch_pages import (
    EpochRead,
    Unavailable,
    assemble_page,
    page_target,
    usable_epochs,
)
from gateway.agents.history.epoch_range import ExternalRange, external_range, in_time_range
from gateway.agents.history.native_reading_window import native_source_identity, native_source_items
from gateway.agents.history.paging import (
    STANDING_HEAD_NOTE_TAGS,
    Row,
    Selection,
    boundary_read_limit,
    depth_allows,
    initial_window,
    older_segment_available,
    window_before,
    window_or_cross,
)
from gateway.agents.history.read_inputs import TimelineRead
from gateway.agents.history.record_timeline import fetch_record_window_sync, record_window_for
from gateway.http.config import gateway_state
from gateway.message_admission.cursor import (
    MAX_CURSOR_LENGTH,
    CursorPart,
    TimelineCursor,
    parse_cursor,
)
from gateway.message_admission.harness import agent_harness
from ops.rpc_schemas import HarnessTimelineWindow, RecordUnreadable

router = APIRouter()
_log = logging.getLogger(__name__)


def _missing_reading_metadata(value: object) -> bool:
    return value is None or value is False


class TimelineResponse(BaseModel):
    """Cold-load timeline payload: one window of rendered items + the
    authoritative `msg_count` (len(state.messages)) + `has_more`.

    `msg_count` is surfaced (not inferred frontend-side from max rendered
    msg_idx) so the merge that preserves the single streaming "future
    partial" uses the exact boundary — a trailing message that renders to
    nothing would otherwise make an inferred count too low.

    The endpoint returns only a tail window (newest `limit` items); `before`
    pages further back for scroll-up history. `has_more` reports whether
    older items exist before the returned window.

    A harness switch ends an epoch and opens the next. The current epoch's items
    keep their unprefixed ids; an earlier epoch's items are prefixed `e<seq>.`
    (`e0.12.0`, `e0.s1.<checkpoint>.3.0`, `e1.h123.0`), its switch block is
    `e<seq>.sw`. A page reads one epoch; the response's `current_epoch`
    is the seq of the epoch whose ids are unprefixed (0 for an agent that never
    switched). `msg_count` describes the current epoch and is 0 on a page of an
    earlier epoch.
    """

    items: list[TimelineItem]
    msg_count: int
    has_more: bool
    current_epoch: int = 0
    reading_source: str | None = Field(default=None, exclude_if=_missing_reading_metadata)


class RetainedTimelineResponse(BaseModel):
    """One retained compact-history window, independent of live state.messages.

    The boundary identity describes the requested retained segment. Historical
    item cursors identify every item's own segment, including a crossed page.
    There is no live message count and these items must not merge into live state.
    """

    boundary_checkpoint_id: str | None
    items: list[TimelineItem]
    has_more: bool


def _read_unavailable(agent_id: int, exc: CheckpointReadError) -> HTTPException:
    _log.warning(
        "timeline checkpoint read unavailable for agent %s: %s (cause %s)",
        agent_id,
        type(exc).__name__,
        type(exc.__cause__).__name__,
    )
    return HTTPException(
        status_code=503, detail=f"Checkpoint history unavailable for agent {agent_id}"
    )


def _load_history_tail(
    db: Database,
    agent_id: int,
    boundary_ids: list[str],
    rank: int,
    limit: int,
    depth: int,
    *,
    read: TimelineRead,
) -> tuple[list[TimelineItem], bool]:
    """Load and window one exact older segment."""
    if rank > len(boundary_ids) or not depth_allows(rank, depth):
        return [], False
    items = _load_history_segment(
        db, agent_id, boundary_ids[rank - 1], rank, read=read, limit=limit
    )
    if items is not None and not items and read.epoch_slice is not None:
        return _load_history_tail(db, agent_id, boundary_ids, rank + 1, limit, depth, read=read)
    if not items:
        return [], False
    window, segment_has_more = tail_window(items, limit)
    return window, segment_has_more or older_segment_available(rank, len(boundary_ids), depth)


def _load_history_segment(
    db: Database,
    agent_id: int,
    checkpoint_id: str,
    rank: int,
    *,
    read: TimelineRead,
    limit: int | None = None,
    before: str | None = None,
) -> list[TimelineItem] | None:
    """Load and render one persisted segment; read/render failures stay visible."""
    _ = before
    if limit is None:
        limit = read.default_limit
    try:
        source = load_checkpoint_timeline_source(
            db, agent_id, read.compact_history, checkpoint_id=checkpoint_id
        )
    except ReadingSourceUnavailableError:
        return None
    except CheckpointReadError as exc:
        raise _read_unavailable(agent_id, exc) from exc
    if not source.messages:
        return None
    prefix = f"s{rank}.{checkpoint_id}"
    return native_source_items(
        source, agent_id, read.rendering, segment_prefix=prefix, epoch_slice=read.epoch_slice
    )


def _load_boundary_ids(db: Database, agent_id: int, depth: int) -> list[str]:
    if depth == 0:
        return []
    try:
        return list_compact_boundary_checkpoint_ids(db, agent_id, limit=boundary_read_limit(depth))
    except CheckpointReadError as exc:
        raise _read_unavailable(agent_id, exc) from exc


def _load_current_message_count(db: Database, agent_id: int) -> int:
    try:
        return load_checkpoint_message_count(db, agent_id)
    except CheckpointReadError as exc:
        raise _read_unavailable(agent_id, exc) from exc


def _historical_window(
    db: Database,
    agent_id: int,
    cursor: TimelineCursor,
    limit: int,
    boundary_ids: list[str],
    depth: int,
    *,
    read: TimelineRead,
) -> tuple[list[TimelineItem], bool]:
    """Page one exact boundary; a stale display rank never selects content."""
    checkpoint_id = cursor.checkpoint_id
    if checkpoint_id is None:
        return [], False
    try:
        rank = boundary_ids.index(checkpoint_id) + 1
    except ValueError:
        return [], False
    if not depth_allows(rank, depth):
        return [], False
    segment_items = _load_history_segment(
        db, agent_id, checkpoint_id, rank, read=read, limit=limit, before=cursor.item_id()
    )
    if not segment_items:
        return [], False
    segment_prefix = f"s{rank}.{checkpoint_id}"
    older_available = older_segment_available(rank, len(boundary_ids), depth)
    page = window_or_cross(
        _rows(segment_items),
        cursor.item_id(segment_prefix),
        limit,
        older_available=older_available,
    )
    head = _picked(segment_items, page)
    if not page.crossed:
        return head, page.has_more
    if not older_available:
        return head, False

    # Release the requested segment before materializing the cross-segment
    # target. At most one checkpoint segment is resident during a request.
    del segment_items
    older_window, older_has_more = _load_history_tail(
        db, agent_id, boundary_ids, rank + 1, limit, depth, read=read
    )
    return [*head, *older_window], older_has_more


def load_current_messages(db: Database, agent_id: int) -> list[BaseMessage]:
    """Load live messages once for timeline and composed snapshot reads.

    Failed storage or reconstruction reads remain unavailable rather than
    masquerading as a successfully empty conversation.
    """
    try:
        return load_checkpoint_messages(db, agent_id)
    except CheckpointReadError as exc:
        raise _read_unavailable(agent_id, exc) from exc


def load_current_source(request: Request, agent_id: int) -> CheckpointTimelineSource:
    try:
        return load_checkpoint_timeline_source(
            gateway_state(request).db, agent_id, request_timeline_read(request).compact_history
        )
    except CheckpointReadError as exc:
        raise _read_unavailable(agent_id, exc) from exc


def load_current_timeline_items(
    db: Database,
    agent_id: int,
    *,
    inputs: TimestampPolicy,
    record_items: Sequence[TimelineItem] = (),
) -> list[TimelineItem]:
    with db.connect() as conn:
        harness = agent_harness(conn, agent_id)
    if harness != "meiva":
        return list(record_items)
    messages = load_current_messages(db, agent_id)
    items, _ = build_timeline_items(messages, inputs=inputs)
    return items


def _get_agent_harness(request: Request, agent_id: int) -> str:
    with gateway_state(request).db_pool.connection() as conn:
        return agent_harness(conn, agent_id)


def _require_agent(request: Request, agent_id: int) -> None:
    """404 for an agent id with no row."""
    with gateway_state(request).db_pool.connection() as conn:
        if not agent_exists(conn, agent_id):
            raise HTTPException(status_code=404, detail=f"agent {agent_id} not found")


def _is_work_item(item: TimelineItem) -> bool:
    return item.kind in {"agent_reasoning", "agent_code", "code_output"} or (
        item.kind == "agent_chat" and item.user_facing is False
    )


def _is_primary_item(item: TimelineItem) -> bool:
    if item.kind == "agent_chat":
        return item.user_facing is not False
    if item.kind == "inbound_chat":
        return item.source == "user"
    return False


# meiva:note
# The facts the windowing rules read of each rendered item, in order.
def _rows(items: Sequence[TimelineItem]) -> list[Row]:
    return [
        Row(
            item.item_id,
            item.kind,
            item.source in STANDING_HEAD_NOTE_TAGS,
            work=_is_work_item(item),
            primary=_is_primary_item(item),
        )
        for item in items
    ]


# meiva:note
# The items a windowing plan selected, in the plan's order.
def _picked(items: Sequence[TimelineItem], selection: Selection) -> list[TimelineItem]:
    return [items[i] for i in selection.indices]


def _head_response(
    agent_id: int,
    source: CheckpointTimelineSource,
    limit: int,
    boundary_ids: list[str],
    *,
    read: TimelineRead,
) -> TimelineResponse:
    """The head window (no cursor) of the current segment."""
    items = native_source_items(source, agent_id, read.rendering, epoch_slice=read.epoch_slice)
    msg_count = len(source.messages)
    tail, tail_has_more = tail_window(items, limit)
    window = initial_window(
        _rows(items),
        tail_size=len(tail),
        tail_has_more=tail_has_more,
        limit=limit,
        historical_segments_available=bool(boundary_ids),
    )
    return TimelineResponse(
        items=_picked(items, window),
        msg_count=msg_count,
        has_more=window.has_more,
        reading_source=encode_reading_source(native_source_identity(source, agent_id)),
    )


# meiva:note
# A composed read reuses its consistent checkpoint view for the default head.
# meiva:link base/agents/history/docs/timeline/timeline.md
def head_timeline(
    agent_id: int, request: Request, source: CheckpointTimelineSource | None
) -> TimelineResponse:
    _require_agent(request, agent_id)
    read = request_timeline_read(request)
    limit = read.default_limit
    harness = _get_agent_harness(request, agent_id)
    epochs = _epochs_of(request, agent_id, harness)
    if epochs:
        return _epoch_response(
            request, agent_id, epochs, limit, None, None, read=read, preloaded_source=source
        )
    if harness != "meiva":
        return _external_harness_response(request, agent_id, harness, limit, None, None)
    return _meiva_harness_response(
        request, agent_id, limit, None, None, read=read, preloaded_source=source
    )


def _current_page_before(
    items: list[TimelineItem],
    cursor: TimelineCursor | None,
    limit: int,
    depth: int,
    boundary_ids: list[str],
    msg_count: int,
) -> TimelineResponse | list[TimelineItem]:
    """A `before=` page of the current segment; the head still to extend with older history
    when the page crosses into a compact-history segment."""
    if cursor is None or cursor.checkpoint_id is not None:
        return TimelineResponse(items=[], msg_count=msg_count, has_more=False)
    # Depth 0 is the compatibility posture: preserve the exact current-
    # segment paging rule, including pages that contain standing context.
    # No boundary query or history branch runs.
    rows = _rows(items)
    if depth == 0:
        window = window_before(rows, cursor.item_id(), limit)
        return TimelineResponse(
            items=_picked(items, window), msg_count=msg_count, has_more=window.has_more
        )
    older_available = bool(boundary_ids) and depth_allows(1, depth)
    page = window_or_cross(rows, cursor.item_id(), limit, older_available=older_available)
    head = _picked(items, page)
    if not page.crossed:
        return TimelineResponse(items=head, msg_count=msg_count, has_more=page.has_more)
    if not older_available:
        return TimelineResponse(items=head, msg_count=msg_count, has_more=False)
    return head


def _historical_response(
    db: Database,
    agent_id: int,
    before: str | None,
    cursor: TimelineCursor | None,
    limit: int,
    boundary_ids: list[str],
    *,
    read: TimelineRead,
) -> TimelineResponse | None:
    """The page of a historical or malformed `before=` request; None for a live-checkpoint read."""
    # Historical pages do not deserialize the live checkpoint. Its exact
    # message count comes from the five-byte channel header instead.
    if before is not None and cursor is None:
        return TimelineResponse(
            items=[], msg_count=_load_current_message_count(db, agent_id), has_more=False
        )
    if cursor is not None and cursor.checkpoint_id is not None:
        window, has_more = _historical_window(
            db, agent_id, cursor, limit, boundary_ids, read.compact_history, read=read
        )
        return TimelineResponse(
            items=window, msg_count=_load_current_message_count(db, agent_id), has_more=has_more
        )
    return None


def _external_harness_response(
    request: Request,
    agent_id: int,
    harness: str,
    limit: int,
    before: str | None,
    cursor: TimelineCursor | None,
) -> TimelineResponse:
    harness_items, harness_count, record_more = record_window_for(
        request, agent_id, harness, limit, before, cursor
    )
    return TimelineResponse(items=harness_items, msg_count=harness_count, has_more=record_more)


def _meiva_harness_response(
    request: Request,
    agent_id: int,
    limit: int,
    before: str | None,
    cursor: TimelineCursor | None,
    *,
    read: TimelineRead,
    preloaded_source: CheckpointTimelineSource | None = None,
) -> TimelineResponse:
    depth = read.compact_history
    db: Database = gateway_state(request).db
    if before is not None and (cursor is None or cursor.checkpoint_id is not None):
        boundary_ids = _load_boundary_ids(db, agent_id, depth)
        early = _historical_response(db, agent_id, before, cursor, limit, boundary_ids, read=read)
        if early is not None:
            return early

    source = (
        preloaded_source if preloaded_source is not None else load_current_source(request, agent_id)
    )
    boundary_ids = list(source.boundaries if depth < 0 else source.boundaries[:depth])
    if before is None:
        head = _head_response(agent_id, source, limit, boundary_ids, read=read)
        if read.epoch_slice is None or head.items or not boundary_ids:
            return head
        older_items, older_more = _load_history_tail(
            db, agent_id, boundary_ids, 1, limit, depth, read=read
        )
        return head.model_copy(update={"items": older_items, "has_more": older_more})
    items_cp = native_source_items(source, agent_id, read.rendering, epoch_slice=read.epoch_slice)
    msg_count = len(source.messages)
    paged = _current_page_before(items_cp, cursor, limit, depth, boundary_ids, msg_count)
    if isinstance(paged, TimelineResponse):
        return paged
    del source, items_cp
    older_window, has_more = _load_history_tail(
        db, agent_id, boundary_ids, 1, limit, depth, read=read
    )
    return TimelineResponse(items=[*paged, *older_window], msg_count=msg_count, has_more=has_more)


def _epochs_of(request: Request, agent_id: int, harness: str) -> list[HarnessEpoch]:
    with gateway_state(request).db_pool.connection() as conn:
        return usable_epochs(read_epochs(conn, agent_id), harness)


def _epoch_native_read(
    request: Request,
    agent_id: int,
    epochs: Sequence[HarnessEpoch],
    seq: int,
    within: TimelineCursor | None,
    limit: int,
    read: TimelineRead,
    preloaded_source: CheckpointTimelineSource | None,
) -> EpochRead:
    local = None if within is None else replace(within, epoch=None, part=CursorPart.CONTENT)
    page = _meiva_harness_response(
        request,
        agent_id,
        limit,
        None if local is None else local.item_id(),
        local,
        read=replace(read, epoch_slice=native_slice_of(epochs, seq)),
        preloaded_source=preloaded_source if seq == len(epochs) - 1 else None,
    )
    return EpochRead(
        page.items,
        has_more=page.has_more,
        msg_count=page.msg_count,
        reading_source=page.reading_source,
    )


_UNREADABLE = {
    RecordUnreadable.NO_SESSION: Unavailable.NO_SESSION,
    RecordUnreadable.FILE_MISSING: Unavailable.FILE_MISSING,
    RecordUnreadable.SESSION_MISMATCH: Unavailable.SESSION_REPLACED,
    RecordUnreadable.UNDECODABLE: Unavailable.UNDECODABLE,
}
_NOT_WRITTEN_YET = frozenset({RecordUnreadable.NO_SESSION, RecordUnreadable.FILE_MISSING})
_DEGRADED_STATUS = {503: Unavailable.MACHINE_OFFLINE, 502: Unavailable.READ_FAILED}


def _epoch_window(
    request: Request,
    agent_id: int,
    epoch: HarnessEpoch,
    span: ExternalRange,
    before: int | None,
    limit: int,
    *,
    current: bool,
) -> HarnessTimelineWindow | Unavailable:
    try:
        return fetch_record_window_sync(
            request,
            agent_id,
            epoch.harness,
            limit=limit,
            before=before,
            floor=span.floor,
            session_id=None if current else epoch.session_id,
        )
    except HTTPException as exc:
        if exc.status_code not in _DEGRADED_STATUS:
            raise
        return _DEGRADED_STATUS[exc.status_code]


def _epoch_external_read(
    request: Request,
    agent_id: int,
    epochs: Sequence[HarnessEpoch],
    seq: int,
    within: TimelineCursor | None,
    limit: int,
) -> EpochRead:
    epoch, current = epochs[seq], seq == len(epochs) - 1
    if not reads_timeline_from_record(epoch.harness):
        return EpochRead([], has_more=False, unavailable=Unavailable.UNSUPPORTED)
    if within is not None and (not within.is_harness or within.checkpoint_id):
        return EpochRead([], has_more=False)
    span = external_range(epochs, epoch)
    before = span.ceiling if within is None else within.msg_idx
    window = _epoch_window(request, agent_id, epoch, span, before, limit, current=current)
    if isinstance(window, Unavailable):
        return EpochRead([], has_more=False, unavailable=window)
    if window.unreadable is not None and not (current and window.unreadable in _NOT_WRITTEN_YET):
        return EpochRead([], has_more=False, unavailable=_UNREADABLE[window.unreadable])
    items = [item for item in window.items if in_time_range(item, span.since, span.until)]
    has_more = window.has_more and (not span.timed or len(items) == len(window.items))
    return EpochRead(items, has_more=has_more, msg_count=window.size)


def _epoch_response(
    request: Request,
    agent_id: int,
    epochs: Sequence[HarnessEpoch],
    limit: int,
    before: str | None,
    cursor: TimelineCursor | None,
    *,
    read: TimelineRead,
    preloaded_source: CheckpointTimelineSource | None = None,
) -> TimelineResponse:
    current = epochs[-1].seq
    target = page_target(epochs, before, cursor)
    if target.seq is None:
        return TimelineResponse(
            items=target.fixed or [],
            msg_count=0,
            has_more=False,
            current_epoch=current,
        )
    if epochs[target.seq].harness == "meiva":
        result = _epoch_native_read(
            request, agent_id, epochs, target.seq, target.within, limit, read, preloaded_source
        )
    else:
        result = _epoch_external_read(request, agent_id, epochs, target.seq, target.within, limit)
    if result.unavailable is not None:
        logger.warning(
            "timeline epoch {epoch} of agent {agent_id} is skipped: {reason}",
            event="timeline_epoch_unreadable",
            agent_id=agent_id,
            epoch=target.seq,
            harness=epochs[target.seq].harness,
            reason=result.unavailable.value,
        )
    page = assemble_page(epochs, target.seq, result)
    return TimelineResponse(
        items=page.items,
        msg_count=page.msg_count,
        has_more=page.has_more,
        current_epoch=current,
        reading_source=page.reading_source,
    )


@router.get("/api/agents/{agent_id}/timeline", dependencies=[Depends(deny_isolated_result_read)])
def get_timeline(
    agent_id: int,
    request: Request,
    # `limit`'s range stays a protective constant (import-time Query bound;
    # task #3696 exception inventory); the default *window* is
    # display.timeline_default_limit.
    limit: int | None = Query(default=None, ge=1, le=1000),
    before: str | None = Query(
        default=None,
        description=(
            "The oldest item id the client holds, verbatim: any id this endpoint returns, including"
            " `e<seq>.sw` of an earlier harness epoch."
        ),
    ),
) -> TimelineResponse:
    """Timeline = raw view of LangGraph state.messages, one window at a time.

    Default (no `before`) returns the newest `limit` items; an omitted
    `limit` resolves to the configured `display.timeline_default_limit`
    (50 by default). Pass
    `before=<oldest item_id you hold>` to fetch the previous window for
    scroll-up history loading. `has_more` reports whether older items exist
    before the returned window.

    One checkpoint segment is built at a time; windowing trims the payload +
    the frontend render. A checkpoint read failure returns 503. Retained
    compact boundaries remain independently readable via /timeline/retained.

    Across harness switches the timeline is one conversation read epoch by epoch.
    The current epoch is served as above with unchanged ids. A `before` cursor at
    the head of an epoch continues into the previous one: its items carry the
    `e<seq>.` id prefix, its start is a `harness_switch` system marker
    `e<seq>.sw`, and a page reads one epoch only. An epoch whose history cannot be
    read (its machine is offline, its record was removed or moved) is skipped
    without an item, with a warning in the gateway log; it never fails the page,
    and the page is its switch block, so paging goes on to the epoch before it.
    Items of an earlier epoch have no `reading_locator`, are not served by
    `/timeline/window`, and cannot be quoted.
    """
    read = request_timeline_read(request)
    if limit is None:
        limit = read.default_limit
    cursor = parse_cursor(before) if before is not None else None
    _require_agent(request, agent_id)
    harness = _get_agent_harness(request, agent_id)
    epochs = _epochs_of(request, agent_id, harness)
    if epochs:
        return _epoch_response(request, agent_id, epochs, limit, before, cursor, read=read)
    if harness != "meiva":
        return _external_harness_response(request, agent_id, harness, limit, before, cursor)
    return _meiva_harness_response(request, agent_id, limit, before, cursor, read=read)


def _retained_cursor(before: str | None, checkpoint_id: str | None) -> TimelineCursor | None:
    if before is None:
        return None
    cursor = parse_cursor(before)
    if cursor is None or cursor.checkpoint_id is None or cursor.epoch is not None:
        raise HTTPException(status_code=400, detail="Retained history requires a historical cursor")
    if checkpoint_id is not None and cursor.checkpoint_id != checkpoint_id:
        raise HTTPException(status_code=400, detail="Retained boundary and cursor do not match")
    return cursor


@router.get(
    "/api/agents/{agent_id}/timeline/retained", dependencies=[Depends(deny_isolated_result_read)]
)
def get_retained_timeline(
    agent_id: int,
    request: Request,
    limit: int | None = Query(default=None, ge=1, le=1000),
    checkpoint_id: str | None = Query(default=None, max_length=MAX_CURSOR_LENGTH),
    before: str | None = Query(default=None, max_length=MAX_CURSOR_LENGTH),
) -> RetainedTimelineResponse:
    """Read retained compact history explicitly, without reading the live head.

    Omit checkpoint_id for the newest retained boundary, or select an exact
    retained boundary. Page using the oldest returned historical item_id.
    Never resumes, rewrites, or repairs the agent's execution checkpoint.
    """
    with gateway_state(request).db_pool.connection() as conn:
        if not agent_exists(conn, agent_id):
            raise HTTPException(status_code=404, detail=f"agent {agent_id} not found")
    db: Database = gateway_state(request).db
    read = request_timeline_read(request)
    depth = read.compact_history
    boundary_ids = _load_boundary_ids(db, agent_id, depth)
    cursor = _retained_cursor(before, checkpoint_id)
    selected = cursor.checkpoint_id if cursor is not None else checkpoint_id
    if selected is None and not boundary_ids:
        return RetainedTimelineResponse(boundary_checkpoint_id=None, items=[], has_more=False)
    selected = selected or boundary_ids[0]
    if selected not in boundary_ids:
        raise HTTPException(status_code=404, detail="Retained compact boundary not found")
    if limit is None:
        limit = read.default_limit
    if cursor is not None:
        items, has_more = _historical_window(
            db, agent_id, cursor, limit, boundary_ids, depth, read=read
        )
    else:
        rank = boundary_ids.index(selected) + 1
        items, has_more = _load_history_tail(
            db, agent_id, boundary_ids, rank, limit, depth, read=read
        )
    return RetainedTimelineResponse(boundary_checkpoint_id=selected, items=items, has_more=has_more)

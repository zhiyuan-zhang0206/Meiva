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

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from langchain_core.messages import BaseMessage
from pydantic import BaseModel

from base.agents.history.checkpoint import (
    CheckpointReadError,
    list_compact_boundary_checkpoint_ids,
    load_checkpoint_message_count,
    load_checkpoint_messages,
    load_checkpoint_messages_segment,
)
from base.agents.history.timeline import TimelineItem, build_timeline_items, tail_window
from base.agents.history.timeline_item import hydrate
from base.agents.messages.timestamp_policy import TimestampPolicy
from base.db import Database, agent_exists
from gateway.agents.eval_guard import deny_isolated_result_read
from gateway.agents.history.configuration import request_timeline_read
from gateway.agents.history.cursor import (
    MAX_CURSOR_LENGTH,
    TimelineCursor,
    item_sort_key,
    parse_cursor,
)
from gateway.agents.history.paging import (
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

router = APIRouter()
_log = logging.getLogger(__name__)


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
    """

    items: list[TimelineItem]
    msg_count: int
    has_more: bool


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
    if limit is None:
        limit = read.default_limit
    try:
        messages = load_checkpoint_messages_segment(db, agent_id, checkpoint_id)
    except CheckpointReadError as exc:
        raise _read_unavailable(agent_id, exc) from exc
    if not messages:
        return None
    prefix = f"s{rank}.{checkpoint_id}"
    items, _ = build_timeline_items(messages, inputs=read.rendering, segment_prefix=prefix)
    return hydrate(db, items, agent_id, limit=limit, before=before)


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


def load_current_timeline_items(
    db: Database, agent_id: int, *, inputs: TimestampPolicy
) -> list[TimelineItem]:
    messages = load_current_messages(db, agent_id)
    items, _ = build_timeline_items(messages, inputs=inputs)
    return items


def _require_agent(request: Request, agent_id: int) -> None:
    """404 for an agent id with no row."""
    with request.app.state.db_pool.connection() as conn:
        if not agent_exists(conn, agent_id):
            raise HTTPException(status_code=404, detail=f"agent {agent_id} not found")


def _current_items(
    request: Request,
    agent_id: int,
    messages: Sequence[BaseMessage],
    limit: int,
    before: str | None,
    *,
    read: TimelineRead,
) -> tuple[list[TimelineItem], int]:
    """The current segment's rendered, hydrated items in item-id order + msg_count."""
    db: Database = request.app.state.db
    items, msg_count = build_timeline_items(messages, inputs=read.rendering)
    items = hydrate(db, items, agent_id, limit=limit, before=before)
    items.sort(key=lambda it: item_sort_key(it.item_id))
    return items, msg_count


# meiva:note
# The facts the windowing rules read of each rendered item, in order.
def _rows(items: Sequence[TimelineItem]) -> list[Row]:
    return [
        Row(item.item_id, item.kind, item.source is not None and item.impersonation is None)
        for item in items
    ]


# meiva:note
# The items a windowing plan selected, in the plan's order.
def _picked(items: Sequence[TimelineItem], selection: Selection) -> list[TimelineItem]:
    return [items[i] for i in selection.indices]


def _head_response(
    request: Request,
    agent_id: int,
    messages: Sequence[BaseMessage],
    limit: int,
    boundary_ids: list[str],
    *,
    read: TimelineRead,
) -> TimelineResponse:
    """The head window (no cursor) of the current segment."""
    items, msg_count = _current_items(request, agent_id, messages, limit, None, read=read)
    tail, tail_has_more = tail_window(items, limit)
    window = initial_window(
        _rows(items),
        tail_size=len(tail),
        tail_has_more=tail_has_more,
        limit=limit,
        historical_segments_available=bool(boundary_ids),
    )
    return TimelineResponse(
        items=_picked(items, window), msg_count=msg_count, has_more=window.has_more
    )


def head_timeline(
    agent_id: int, request: Request, messages: Sequence[BaseMessage]
) -> TimelineResponse:
    """The default head window ``GET .../timeline`` serves, from loaded messages.

    For a composed read that already holds the live checkpoint's messages
    (``load_current_messages``) so it deserializes the checkpoint once.
    """
    _require_agent(request, agent_id)
    read = request_timeline_read(request)
    boundary_ids = _load_boundary_ids(request.app.state.db, agent_id, read.compact_history)
    return _head_response(request, agent_id, messages, read.default_limit, boundary_ids, read=read)


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


@router.get("/api/agents/{agent_id}/timeline", dependencies=[Depends(deny_isolated_result_read)])
def get_timeline(
    agent_id: int,
    request: Request,
    # `limit`'s range stays a protective constant (import-time Query bound;
    # task #3696 exception inventory); the default *window* is
    # display.timeline_default_limit.
    limit: int | None = Query(default=None, ge=1, le=1000),
    before: str | None = Query(default=None),
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
    """
    read = request_timeline_read(request)
    if limit is None:
        limit = read.default_limit
    cursor = parse_cursor(before) if before is not None else None
    _require_agent(request, agent_id)
    depth = read.compact_history
    db: Database = request.app.state.db
    boundary_ids = _load_boundary_ids(db, agent_id, depth)

    early = _historical_response(db, agent_id, before, cursor, limit, boundary_ids, read=read)
    if early is not None:
        return early

    messages = load_current_messages(db, agent_id)
    if before is None:
        return _head_response(request, agent_id, messages, limit, boundary_ids, read=read)
    items, msg_count = _current_items(request, agent_id, messages, limit, before, read=read)
    paged = _current_page_before(items, cursor, limit, depth, boundary_ids, msg_count)
    if isinstance(paged, TimelineResponse):
        return paged
    del messages, items
    older_window, has_more = _load_history_tail(
        db, agent_id, boundary_ids, 1, limit, depth, read=read
    )
    return TimelineResponse(items=[*paged, *older_window], msg_count=msg_count, has_more=has_more)


def _retained_cursor(before: str | None, checkpoint_id: str | None) -> TimelineCursor | None:
    if before is None:
        return None
    cursor = parse_cursor(before)
    if cursor is None or cursor.checkpoint_id is None:
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
    with request.app.state.db_pool.connection() as conn:
        if not agent_exists(conn, agent_id):
            raise HTTPException(status_code=404, detail=f"agent {agent_id} not found")
    db: Database = request.app.state.db
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

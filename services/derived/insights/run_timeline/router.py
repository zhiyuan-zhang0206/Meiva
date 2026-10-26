# meiva:link services/derived/insights/run_timeline/docs/range-projection.meiva.okf.md
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from base.agents.messages.timestamp_policy import TimestampSource
from base.db.protocols import DatabaseLike as Database
from services.derived.insights.config import InsightsConfig
from services.derived.insights.run_timeline.context import (
    ResolvedRequest,
    build_run_timeline_context,
)
from services.derived.insights.run_timeline.links import read as read_links
from services.derived.insights.run_timeline.links import read_content as read_link_content
from services.derived.insights.run_timeline.messages import (
    LIMIT_MAX,
    MessageSliceSpec,
    slice_messages,
)
from services.derived.insights.run_timeline.projection.reader import TimelineReader
from services.derived.insights.run_timeline.schemas import (
    RunTimelineContext,
    RunTimelineLinkContent,
    RunTimelineLinks,
    RunTimelineMessages,
    RunTimelineResponse,
    RunTimelineWindow,
)

router = APIRouter()


_EMPTY_WINDOW = timedelta(hours=24)


def _window(
    lifetime: tuple[datetime, datetime] | None,
    from_: datetime | None,
    to: datetime | None,
    now: datetime,
    recent_hours: int | None = None,
) -> tuple[datetime, datetime]:
    for name, value in (("from", from_), ("to", to)):
        if value is not None and value.tzinfo is None:
            raise HTTPException(status_code=422, detail=f"{name} must include a timezone offset")
    default = lifetime or (now - _EMPTY_WINDOW, now)
    end = to or default[1]
    start = from_ or (
        max(default[0], end - timedelta(hours=recent_hours))
        if recent_hours is not None
        else default[0]
    )
    if start >= end:
        raise HTTPException(status_code=422, detail="from must be earlier than to")
    return start, end


def _options(
    max_units: Annotated[int | None, Query(ge=1, le=10000)] = None,
    snapshot: Annotated[str | None, Query(min_length=1, max_length=64)] = None,
) -> tuple[int | None, str | None]:
    return max_units, snapshot


def _message_options(
    limit: Annotated[int, Query(ge=1, le=LIMIT_MAX)] = 50,
    *,
    full: Annotated[bool, Query()] = False,
    snapshot: Annotated[str | None, Query(min_length=1, max_length=64)] = None,
) -> tuple[int, bool, str | None]:
    return limit, full, snapshot


@router.get(
    "/api/agents/{agent_id}/run-timeline",
    description="A time window; bounded reads omit dense message detail and name its snapshot.",
)
def get_run_timeline(
    request: Request,
    agent_id: int,
    from_: Annotated[datetime | None, Query(alias="from")] = None,
    to: Annotated[datetime | None, Query()] = None,
    recent_hours: Annotated[int | None, Query(ge=1, le=168)] = None,
    options: Annotated[tuple[int | None, str | None], Depends(_options)] = (None, None),
) -> RunTimelineResponse:
    max_units, snapshot = options
    reader: TimelineReader = request.app.state.run_timeline_reader
    timestamps: TimestampSource = request.app.state.timeline_timestamps
    if recent_hours is not None and (from_ is not None or to is not None):
        raise HTTPException(status_code=422, detail="recent_hours cannot be combined with from/to")
    for name, value in (("from", from_), ("to", to)):
        if value is not None and value.tzinfo is None:
            raise HTTPException(422, f"{name} must include a timezone offset")
    if from_ is not None and to is not None and from_ >= to:
        raise HTTPException(422, "from must be earlier than to")
    cut = reader.get(agent_id, snapshot)
    start, end = _window(
        cut.lifetime,
        from_,
        to,
        timestamps.clock.now(),
        recent_hours,
    )
    nodes, units, omitted = reader.window(cut, start, end, max_units)
    return RunTimelineResponse(
        agent_id=agent_id,
        window=RunTimelineWindow(from_=start, to=end),
        lifetime=RunTimelineWindow(from_=cut.lifetime[0], to=cut.lifetime[1])
        if cut.lifetime
        else None,
        nodes=nodes,
        units=units,
        units_omitted=omitted,
        snapshot=cut.id,
    )


@router.get(
    "/api/agents/{agent_id}/run-timeline/messages",
    description="Messages ``start..end`` of the agent's stitched history, at most ``limit`` of them.",
)
def get_run_timeline_messages(
    request: Request,
    agent_id: int,
    start: Annotated[int, Query(ge=0)],
    end: Annotated[int, Query(ge=0)],
    options: Annotated[tuple[int, bool, str | None], Depends(_message_options)] = (
        50,
        False,
        None,
    ),
) -> RunTimelineMessages:
    limit, full, snapshot = options
    if end < start:
        raise HTTPException(status_code=422, detail="end must not be before start")
    config: InsightsConfig = request.app.state.insights_config
    reader: TimelineReader = request.app.state.run_timeline_reader
    cut = reader.get(agent_id, snapshot)
    if end >= cut.count:
        raise HTTPException(404, f"message {end} not found: history has {cut.count}")
    stop = min(end, start + limit - 1)
    timestamps: TimestampSource = request.app.state.timeline_timestamps
    return slice_messages(
        reader.span(cut, start, stop),
        MessageSliceSpec(
            start=start,
            stop=stop,
            end=end,
            full=full,
            text_max=config.run_timeline_message_text_max,
            inputs=timestamps.policy(),
        ),
    )


@router.get(
    "/api/agents/{agent_id}/run-timeline/context",
    description="The context breakdown of the LLM request at (or next after) message index `at`.",
)
def get_run_timeline_context(
    request: Request,
    agent_id: int,
    at: Annotated[int, Query(ge=0)],
    snapshot: Annotated[str | None, Query(min_length=1, max_length=64)] = None,
) -> RunTimelineContext:
    reader: TimelineReader = request.app.state.run_timeline_reader
    cut = reader.get(agent_id, snapshot)
    idx, ts, segment, view = reader.request(cut, at)
    config: InsightsConfig = request.app.state.insights_config
    return build_run_timeline_context(
        request.app.state.db_pool,
        agent_id,
        ResolvedRequest(
            idx=idx,
            ts=ts,
            session=segment.session,
            offset=segment.offset,
            sessions=cut.sessions,
            view=view,
        ),
        catalog=request.app.state.catalog,
        default_model=config.default_model,
    )


@router.get(
    "/api/insights/run-timeline/links",
    description="Agent-to-agent events in the window with at least one end among the comma-separated `agents`.",
)
def get_run_timeline_links(
    request: Request,
    agents: Annotated[str, Query()],
    from_: Annotated[datetime, Query(alias="from")],
    to: Annotated[datetime, Query()],
) -> RunTimelineLinks:
    try:
        ids = sorted({int(part) for part in agents.split(",")})
    except ValueError:
        raise HTTPException(
            status_code=422, detail="agents must be comma-separated integers"
        ) from None
    if from_.tzinfo is None or to.tzinfo is None:
        raise HTTPException(status_code=422, detail="from and to must include a timezone offset")
    if from_ >= to:
        raise HTTPException(status_code=422, detail="from must be earlier than to")
    db: Database = request.app.state.db
    return RunTimelineLinks(links=read_links(db, ids, from_, to))


@router.get(
    "/api/insights/run-timeline/link-content",
    description="The full text of one chat message (`inbound_id`) or the title and text of one notice (`notice_id`); exactly one.",
)
def get_run_timeline_link_content(
    request: Request,
    inbound_id: Annotated[int | None, Query(ge=1)] = None,
    notice_id: Annotated[int | None, Query(ge=1)] = None,
) -> RunTimelineLinkContent:
    if (inbound_id is None) == (notice_id is None):
        raise HTTPException(status_code=422, detail="give exactly one of inbound_id and notice_id")
    db: Database = request.app.state.db
    found = read_link_content(db, inbound_id=inbound_id, notice_id=notice_id)
    if found is None:
        raise HTTPException(status_code=404, detail="no such message or notice")
    return found

# meiva:link services/derived/insights/run_timeline/docs/range-projection.meiva.okf.md
from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from psycopg_pool import ConnectionPool
from starlette.applications import Starlette
from starlette.datastructures import State

from base.agents.messages.timestamp_policy import TimestampSource
from base.db.protocols import DatabaseLike as Database
from base.lm.catalog import ModelCatalog
from services.derived.insights.config import InsightsConfig
from services.derived.insights.run_timeline.context import (
    ResolvedRequest,
    build_run_timeline_context,
)
from services.derived.insights.run_timeline.links import (
    check_link_content_selector,
    check_link_window,
    parse_agent_ids,
)
from services.derived.insights.run_timeline.links import read as read_links
from services.derived.insights.run_timeline.links import read_content as read_link_content
from services.derived.insights.run_timeline.messages import (
    LIMIT_MAX,
    MessageSliceSpec,
    slice_messages,
)
from services.derived.insights.run_timeline.projection.reader import TimelineReader
from services.derived.insights.run_timeline.request_rules import (
    TimelineRequestError,
    check_message_range,
    check_window_query,
    message_page_stop,
    resolve_window,
)
from services.derived.insights.run_timeline.schemas import (
    RunTimelineContext,
    RunTimelineLinkContent,
    RunTimelineLinks,
    RunTimelineMessages,
    RunTimelineResponse,
    RunTimelineWindow,
)

router = APIRouter()


# meiva:note
# What the insights app carries on `app.state`: the one set of attributes its handlers read. The app
# is built with an instance of this class (`build_app`), and `insights_state` reaches it.
class InsightsAppState(State):
    db: Database
    db_pool: ConnectionPool
    insights_config: InsightsConfig
    catalog: ModelCatalog
    timeline_timestamps: TimestampSource
    run_timeline_reader: TimelineReader


# meiva:note
# The state of the app serving this request. A state of any other class is a wiring error (an app
# built without `build_app`), so it fails here instead of as a missing attribute in a handler.
def insights_state(request: Request) -> InsightsAppState:
    app = request.scope.get("app")
    state = app.state if isinstance(app, Starlette) else None
    if not isinstance(state, InsightsAppState):
        raise TypeError("the app's state is not an InsightsAppState; build the app with build_app")
    return state


# meiva:note
# The HTTP face of the pure refusals in `request_rules`: every route body runs inside it.
@contextmanager
def _request_errors() -> Generator[None]:
    try:
        yield
    except TimelineRequestError as refusal:
        raise HTTPException(refusal.status, refusal.detail, headers=refusal.headers) from None


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
    state = insights_state(request)
    reader, timestamps = state.run_timeline_reader, state.timeline_timestamps
    with _request_errors():
        check_window_query(from_, to, recent_hours)
        cut = reader.get(agent_id, snapshot)
        start, end = resolve_window(cut.lifetime, from_, to, timestamps.clock.now(), recent_hours)
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
    state = insights_state(request)
    config, reader, timestamps = (
        state.insights_config,
        state.run_timeline_reader,
        state.timeline_timestamps,
    )
    with _request_errors():
        check_message_range(start, end)
        cut = reader.get(agent_id, snapshot)
        stop = message_page_stop(start, end, limit, cut.count)
        span = reader.span(cut, start, stop)
    return slice_messages(
        span,
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
    state = insights_state(request)
    reader = state.run_timeline_reader
    with _request_errors():
        cut = reader.get(agent_id, snapshot)
        idx, ts, segment, view = reader.request(cut, at)
    config = state.insights_config
    return build_run_timeline_context(
        state.db_pool,
        agent_id,
        ResolvedRequest(
            idx=idx,
            ts=ts,
            session=segment.session,
            offset=segment.offset,
            sessions=cut.sessions,
            view=view,
        ),
        catalog=state.catalog,
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
    with _request_errors():
        ids = parse_agent_ids(agents)
        check_link_window(from_, to)
    db = insights_state(request).db
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
    with _request_errors():
        check_link_content_selector(inbound_id, notice_id)
    db = insights_state(request).db
    found = read_link_content(db, inbound_id=inbound_id, notice_id=notice_id)
    if found is None:
        raise HTTPException(status_code=404, detail="no such message or notice")
    return found

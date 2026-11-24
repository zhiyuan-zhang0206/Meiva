"""Wire models of the event-stream reads: unified and per-agent event rows,
the class-level event-resolution API (task #1468), and the aggregate metrics
reports over the stream.

FastAPI registers these unchanged, so the OpenAPI codegen is byte-identical to
the wire before.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    NonNegativeInt,
)

from base.events.contract import EventGroupBy, EventTier


class AgentEventRow(BaseModel):
    """One row from the unified event stream — admin event log entry (category=telemetry/log).

    `agent_id` is None for service-level lines (gateway / scheduler / labeler
    / runner / etc.) and an int for agent process lines. `payload` keeps the
    full structured record (msg, kwargs from logger.bind / kwargs from the
    call, plus traceback fields when an exception was attached).
    `level` is LOWERCASE (`debug`/`info`/`warning`/`error`/`critical`)."""

    model_config = ConfigDict(frozen=True)

    id: int
    line_sha256: str
    ts: datetime
    agent_id: int | None
    level: str
    event: str
    payload: dict[str, object]


class AgentEventsResponse(BaseModel):
    """GET /api/cluster/admin/events response — newest-first slice."""

    model_config = ConfigDict(frozen=True)

    items: list[AgentEventRow]


class EventRow(BaseModel):
    """One row of the unified event stream.

    Every signal shares this shape (event-system design doc §1): audit,
    telemetry and log all land in it, written through the unified emitter
    (`base/telemetry/emitter.py`).
    `trace_id` is the correlation key — one turn = one trace id, every event
    inside it carries the same value. `agent_id` is None for service-level
    events (gateway / daemons); `machine` is the host dimension. `level` is
    LOWERCASE (`debug`/`info`/`warning`/`error`/`critical`)."""

    model_config = ConfigDict(frozen=True)

    id: int
    line_sha256: str
    ts: datetime
    trace_id: str | None
    span_id: str | None
    agent_id: int | None
    machine: str
    process: str
    category: str
    event_name: str
    tier: EventTier
    level: str
    source: str
    target_agent_id: int | None
    attributes: dict[str, object]


class EventsMeta(BaseModel):
    """GET /api/events response header — effective window + pagination
    state. `window_from`/`window_to` echo the applied time window
    (`None` when unbounded high; `window_from` is never `None` — the
    computed `now - hours` when the request used `hours`, the explicit
    `from` when one was given, and otherwise the default lower bound
    `now - 24h`). `has_more` (from the list fetch's +1 lookahead) tells a
    paging client whether another `offset` page exists. `total` is the exact
    filtered row count before paging, computed only when the request asked
    for it (`with_total=1`) — `None` otherwise (it costs a full-window count
    aggregation)."""

    model_config = ConfigDict(frozen=True)

    total: int | None
    window_from: datetime | None
    window_to: datetime | None
    limit: int
    offset: int
    has_more: bool
    generated_at: str  # ISO-8601 UTC


class EventsResponse(BaseModel):
    """GET /api/events response — newest-first slice of the unified event
    stream (`ts DESC`, `id DESC` tiebreak so `limit`/`offset` paging is stable
    across same-`ts` rows)."""

    model_config = ConfigDict(frozen=True)

    meta: EventsMeta
    items: list[EventRow]


# meiva:note
# One group of `GET /api/events/counts`: the dimension's value (`agent_id` as its decimal text;
# None for service events that belong to no agent) and how many events carry it.
class EventCountGroup(BaseModel):
    model_config = ConfigDict(frozen=True)

    key: str | None
    count: int


# meiva:note
# GET /api/events/counts header: the dimension, the effective window and the totals. `total` is the
# event count over every group (not only the returned ones); `truncated` says that more groups
# exist than `limit` returned.
class EventCountsMeta(BaseModel):
    model_config = ConfigDict(frozen=True)

    by: EventGroupBy
    window_from: datetime | None
    window_to: datetime | None
    limit: int
    total: int
    truncated: bool
    generated_at: str


# meiva:note
# GET /api/events/counts response: events of the unified stream counted per value of one
# dimension, largest group first (ties by key).
class EventCountsResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    meta: EventCountsMeta
    groups: list[EventCountGroup]


class MetricsMeta(BaseModel):
    """`/api/metrics/agents` report header — window + provenance."""

    model_config = ConfigDict(frozen=True)

    window_days: int
    generated_at: str  # ISO-8601 UTC; frontend renders in local time
    distinct_agents: NonNegativeInt


class AgentMetricsItem(BaseModel):
    """One agent's row in the fleet metrics breakdown — headline usage
    aggregated over the report window. `label` is the
    agent's display name (None = unset; frontend falls back to "#id").
    `cost_usd` sums recorded usage-time price snapshots; unpriced calls contribute 0.
    `cache_hit_pct` uses only calls reporting cache usage, and is null when nonzero
    input has no cache metering."""

    model_config = ConfigDict(frozen=True)

    agent_id: int
    label: str | None
    cost_usd: float = Field(ge=0)
    cost_usd_api: float = Field(default=0, ge=0)
    cost_usd_subscription: float = Field(default=0, ge=0)
    usage_incomplete_reports: NonNegativeInt = 0
    usage_unpriced_calls: NonNegativeInt = 0
    llm_calls: NonNegativeInt
    tokens_in: NonNegativeInt
    tokens_out: NonNegativeInt
    tokens_cached: NonNegativeInt
    cache_hit_pct: float | None = Field(ge=0, le=100)


class AgentMetricsReport(BaseModel):
    """GET /api/metrics/agents response — the per-agent breakdown behind the
    metrics page's Agents panel, sorted by cost descending (ties by agent_id)."""

    model_config = ConfigDict(frozen=True)

    meta: MetricsMeta
    agents: list[AgentMetricsItem]

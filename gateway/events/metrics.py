"""Per-agent metrics report — `GET /api/metrics/agents`, behind `meiva metrics agents`.

One headline-counter row per agent over a window. The window is reduced in SQL
(`base.telemetry.metrics.aggregate.fetch_agent_rollups`; a few statements in one connection,
nothing materialized per row), window-selected and re-aggregated on every call (no caching),
under a statement timeout (503 on timeout).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

import psycopg
from fastapi import APIRouter, HTTPException, Query, Request

from base.db.rows import query_all
from base.telemetry.metrics.aggregate import fetch_agent_rollups
from gateway.events.schemas import AgentMetricsItem, AgentMetricsReport, MetricsMeta
from gateway.http.config import gateway_clock, gateway_config, gateway_state

router = APIRouter()

_READ_TIMEOUT = "SET LOCAL statement_timeout = '20s'"


# meiva:note
# An agent's id and display label (NULL while unnamed).
@dataclass(frozen=True)
class _AgentLabelRow:
    id: int
    label: str | None


@router.get("/api/metrics/agents")
def get_metrics_agents(
    request: Request,
    # `days`'s range stays a protective constant (import-time Query bound;
    # task #3696 exception inventory); the default *window* is
    # display.metrics_default_window_days.
    days: Annotated[int | None, Query(ge=1, le=30)] = None,
    since_compact: Annotated[bool, Query()] = False,  # noqa: FBT002 — FastAPI query param
) -> AgentMetricsReport:
    """Per-agent breakdown of the last `days` of events — one
    headline-counter row per agent (cost / tokens / cache hit / turn + exec
    outcomes), sorted by cost descending. `since_compact=true` narrows each
    agent's events to those at or after its latest compact halt. Service-level
    events (no agent_id) count toward `meta.total_events` but produce no row —
    and the count covers every telemetry/log event in the window (all
    processes)."""
    if days is None:
        days = int(gateway_config(request).display.metrics_default_window_days)
    now = gateway_clock(request).now()
    try:
        with gateway_state(request).db_pool.connection() as conn:
            conn.execute(_READ_TIMEOUT)
            total_events, rollups = fetch_agent_rollups(
                conn, days, now=now, since_compact=since_compact
            )
            labels: dict[int, str | None] = {}
            if rollups:
                labelled = query_all(
                    conn,
                    _AgentLabelRow,
                    "SELECT id, label FROM agents WHERE id = ANY(%s)",
                    (list(rollups),),
                )
                labels = {row.id: row.label for row in labelled}
    except psycopg.errors.QueryCanceled as exc:
        raise HTTPException(status_code=503, detail="metrics read timed out; retry") from exc
    items = [
        # telemetry_events.agent_id has no FK: a deleted agent's rows keep their id, label None.
        AgentMetricsItem.model_validate({**rollup, "agent_id": aid, "label": labels.get(aid)})
        for aid, rollup in rollups.items()
    ]
    items.sort(key=lambda item: (-item.cost_usd, item.agent_id))
    meta = MetricsMeta(
        window_days=days,
        generated_at=now.isoformat(),
        total_events=total_events,
        distinct_agents=len(rollups),
        since_compact=since_compact,
    )
    return AgentMetricsReport(meta=meta, agents=items)

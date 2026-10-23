# meiva:note
# Ops monitor panel — `GET /api/ops/monitor`.
#
# One round trip backs the whole Insights Ops section: time-bucketed series
# for the three MVP metric groups (SSE/event-log backlog, LLM latency + TPS,
# service start counts) plus whole-window totals and breakdowns. Series are
# computed from `telemetry_events` (`gateway/cluster/ops_series`): counts and sums
# are exact, LLM p50/p95/max latency are exact over the rows of each bucket.
# Window is capped at 7d.
#
# Adding a metric: new event emissions at the collection point + a read-out in
# `gateway/cluster/ops_series` + one schema here + one frontend panel.

from __future__ import annotations

from typing import Annotated, Literal

import psycopg
from fastapi import APIRouter, HTTPException, Query, Request

from gateway.cluster.monitor.ops_series import fetch_ops_series
from gateway.cluster.monitor.stats_events import set_statement_timeout
from gateway.cluster.schemas import OpsMonitorReport
from gateway.http.config import GatewayConfig

router = APIRouter()


@router.get(
    "/api/ops/monitor",
    description="Time-bucketed ops series over `window` (default 24h). Bucket width is\nderived from the window (1h→60s, 6h→300s, 24h→1800s, 7d→3600s); every\nbucket in the window is present (zero-filled when empty), aligned to\n`meta.bucket_starts`.",
)
def get_ops_monitor(
    request: Request,
    window: Annotated[Literal["1h", "6h", "24h", "7d"], Query()] = "24h",
) -> OpsMonitorReport:
    config: GatewayConfig = request.app.state.gateway_config
    try:
        with request.app.state.db_pool.connection() as conn:
            set_statement_timeout(conn, config.stats_statement_timeout_ms)
            data = fetch_ops_series(conn, window)
    except psycopg.errors.QueryCanceled as exc:
        raise HTTPException(status_code=503, detail="ops monitor read timed out; retry") from exc
    return OpsMonitorReport(**data)

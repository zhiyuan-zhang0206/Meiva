# meiva:link services/derived/insights/docs/insights.meiva.okf.md
from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from functools import partial
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from psycopg_pool import ConnectionPool

from base.agents.history.checkpoint_segment import read_checkpoint_segment
from base.agents.messages.timestamp_policy import TimestampSource
from base.clock import SYSTEM_TIMERS
from base.db import Database
from base.lm.catalog import ModelCatalog
from services.derived.insights.config import InsightsConfig
from services.derived.insights.run_timeline import router as run_timeline_router
from services.derived.insights.run_timeline.projection.jobs import IndexJobs, index_next
from services.derived.insights.run_timeline.projection.reader import TimelineReader


def build_app(
    db: Database,
    pool: ConnectionPool[Any],
    config: InsightsConfig,
    *,
    home: Path,
    catalog: ModelCatalog,
    timestamps: TimestampSource,
    pid: int = 0,
) -> FastAPI:
    jobs = IndexJobs(
        partial(index_next, db, timestamps=timestamps, read_segment=read_checkpoint_segment)
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
        try:
            yield
        finally:
            jobs.close()

    app = FastAPI(
        title="meiva-insights", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan
    )
    app.state.db = db
    app.state.db_pool = pool
    app.state.insights_config = config
    app.state.catalog = catalog
    app.state.timeline_timestamps = timestamps
    app.state.run_timeline_reader = TimelineReader(
        db,
        pool,
        timestamps=timestamps,
        jobs=jobs,
        read_segment=read_checkpoint_segment,
        timers=SYSTEM_TIMERS,
    )
    app.include_router(run_timeline_router.router)

    @app.get(
        "/healthz",
        description="Identity for the supervisor's probe: which service, which home, which process.",
    )
    async def healthz() -> dict[str, object]:
        return {"name": "insights", "pid": pid, "home": str(home)}

    return app

# meiva:link services/derived/insights/docs/insights.meiva.okf.md
from __future__ import annotations

import random
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from psycopg_pool import ConnectionPool

from base.agents.history.checkpoint_segment import read_checkpoint_segment
from base.agents.messages.timestamp_policy import TimestampSource
from base.clock import Timers
from base.db import Database
from base.lm.catalog import ModelCatalog
from services.derived.insights.config import InsightsConfig
from services.derived.insights.run_timeline import router as run_timeline_router
from services.derived.insights.run_timeline.projection.jobs import IndexJobs, SegmentReads
from services.derived.insights.run_timeline.projection.reader import TimelineReader
from services.derived.insights.run_timeline.router import InsightsAppState


def build_app(
    db: Database,
    pool: ConnectionPool,
    config: InsightsConfig,
    *,
    home: Path,
    catalog: ModelCatalog,
    timestamps: TimestampSource,
    timers: Timers,
    rng: random.Random,
    pid: int = 0,
) -> FastAPI:
    reads = SegmentReads(db, timestamps, read_checkpoint_segment)
    jobs = IndexJobs(reads.index_next)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
        try:
            yield
        finally:
            jobs.close()

    app = FastAPI(
        title="meiva-insights", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan
    )
    state = InsightsAppState()
    state.db = db
    state.db_pool = pool
    state.insights_config = config
    state.catalog = catalog
    state.timeline_timestamps = timestamps
    state.run_timeline_reader = TimelineReader(reads, pool, jobs=jobs, timers=timers, rng=rng)
    app.state = state
    app.include_router(run_timeline_router.router)

    @app.get(
        "/healthz",
        description="Identity for the supervisor's probe: which service, which home, which process.",
    )
    async def healthz() -> dict[str, object]:
        return {"name": "insights", "pid": pid, "home": str(home)}

    return app

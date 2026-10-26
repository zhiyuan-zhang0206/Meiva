# meiva:link services/derived/insights/run_timeline/docs/range-projection.meiva.okf.md
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime

from base.agents.history.hierarchy.store import load_generation_costs
from base.db.protocols import DatabaseLike
from services.derived.insights.run_timeline.projection.model import Fact, NodeRow
from services.derived.insights.run_timeline.schemas import (
    RunTimelineGeneration,
    RunTimelineNode,
    RunTimelineUsage,
)


def node_bounds(
    low: int | None,
    high: int | None,
    tail: list[Fact],
    start: datetime,
    end: datetime,
) -> tuple[int, int] | None:
    lows = [f.idx for f in tail if f.end_ts is not None and f.end_ts >= start]
    highs = [f.idx for f in tail if f.start_ts is not None and f.start_ts <= end]
    if low is not None:
        lows.append(low)
    if high is not None:
        highs.append(high)
    return (min(lows), max(highs)) if lows and highs and min(lows) <= max(highs) else None


def project_nodes(
    rows: list[NodeRow],
    jobs: Mapping[int, RunTimelineGeneration],
    checks: Mapping[str, RunTimelineGeneration],
) -> list[RunTimelineNode]:
    out: list[RunTimelineNode] = []
    for row in rows:
        before, after = row.before, row.after
        usage = {
            name: getattr(after, name) - getattr(before, name)
            for name in RunTimelineUsage.model_fields
        }
        usage["cost_usd"] = round(usage["cost_usd"], 9)
        generation = (
            jobs.get(row.job_id)
            if row.depth == 1 and row.job_id is not None
            else checks.get(row.check_key)
            if row.depth != 1 and row.check_key is not None
            else None
        )
        known = after.known > before.known
        out.append(
            RunTimelineNode(
                id=str(row.id),
                level=row.depth,
                parent=str(row.parent_id) if row.parent_id else None,
                start=row.start,
                end=row.end,
                span_start=row.span_start,
                span_end=row.span_end,
                summary=row.text,
                usage=RunTimelineUsage.model_validate(usage),
                generation=generation,
                context_tokens=after.context - before.context if known else None,
                estimated=after.estimated > before.estimated if known else None,
            )
        )
    return out


def window_nodes(
    db: DatabaseLike,
    agent_id: int,
    rows: list[NodeRow],
) -> list[RunTimelineNode]:
    if not rows:
        return []
    jobs, checks = load_generation_costs(
        db,
        agent_id,
        job_ids=sorted({r.job_id for r in rows if r.job_id is not None}),
        check_keys=sorted({r.check_key for r in rows if r.check_key is not None}),
    )
    return project_nodes(
        rows,
        {k: RunTimelineGeneration(**vars(v)) for k, v in jobs.items()},
        {k: RunTimelineGeneration(**vars(v)) for k, v in checks.items()},
    )

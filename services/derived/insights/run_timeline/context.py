# meiva:link services/derived/insights/run_timeline/docs/range-projection.meiva.okf.md
from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from datetime import datetime

from langchain_core.messages import AIMessage

from base.agents.history.context_breakdown import request_breakdown
from base.agents.history.context_response import context_breakdown_response
from base.db.protocols import SyncPool
from base.lm.catalog import ModelCatalog
from services.derived.insights.run_timeline.history import HistoryView
from services.derived.insights.run_timeline.schemas import RunTimelineContext


@dataclass(frozen=True)
class LlmRequest:
    idx: int
    ts: datetime
    session: int


def llm_requests(view: HistoryView) -> list[LlmRequest]:
    starts = view.history.segment_starts
    out: list[LlmRequest] = []
    for idx, msg in enumerate(view.history.messages):
        if not isinstance(msg, AIMessage) or not msg.usage_metadata:
            continue
        sent = view.read[idx - 1] if idx > 0 and view.read[idx - 1] is not None else view.read[idx]
        if sent is not None:
            out.append(LlmRequest(idx, sent, max(bisect_right(starts, idx) - 1, 0)))
    return out


def request_at(requests: list[LlmRequest], at: int) -> LlmRequest | None:
    if not requests:
        return None
    found = bisect_left([r.idx for r in requests], at)
    return requests[min(found, len(requests) - 1)]


@dataclass(frozen=True)
class ResolvedRequest:
    idx: int
    ts: datetime
    session: int
    offset: int
    sessions: int
    view: HistoryView


def build_run_timeline_context(
    pool: SyncPool,
    agent_id: int,
    resolved: ResolvedRequest,
    *,
    catalog: ModelCatalog,
    default_model: str,
) -> RunTimelineContext:
    local = resolved.idx - resolved.offset
    history = resolved.view.history
    body_start = history.segment_starts[0]
    breakdown = context_breakdown_response(
        pool,
        agent_id,
        request_breakdown(
            history.segment_heads[0],
            history.messages[body_start : local + 1],
            resolved.view.segments[0],
            local - body_start,
        ),
        catalog=catalog,
        default_model=default_model,
    )
    return RunTimelineContext(
        **breakdown.model_dump(),
        request=resolved.idx,
        session=resolved.session,
        sessions=resolved.sessions,
        ts=resolved.ts,
    )

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime

from base.agents.context import MeivaContext
from base.agents.history.timeline_item import TurnTimingSpan, apply_turn_timings
from base.agents.history.transcript_timeline.project import ProjectedItem
from base.agents.model.harness_models import resolve_harness_catalog_model
from base.events.live.projection import TimelineSnapshot
from base.events.live.projection import TurnStarted as ProjectionTurnStarted
from base.events.live.publisher import AgentEventPublisher
from base.lm.catalog import ModelCatalog
from base.lm.harness.engine import HarnessTurn
from base.lm.harness.registry import TurnTimingStore, timeline_turn_id
from base.lm.usage import UsageFields, log_usage_fields
from base.log import logger
from base.paths import harness_sessions_dir


# meiva:note
# Usage is logged where every other agent turn logs it, with the harness name as the
# `source` so the two runtimes stay comparable. A turn whose provider reported no
# tokens is not logged at all: an all-zero row would read as a free call rather than
# as a provider that said nothing.
# meiva:link agent/graph/docs/harness-turn.md
def resolve_pricing_model(raw_model: str, catalog: ModelCatalog) -> str:
    return resolve_harness_catalog_model(raw_model, catalog.prices)


def log_turn_usage(ctx: MeivaContext, agent_id: int, model: str | None, turn: HarnessTurn) -> None:
    usage = turn.usage
    if usage.input_tokens == 0 and usage.output_tokens == 0:
        return
    catalog = ctx.require_catalog()
    pricing_model = resolve_pricing_model(model or ctx.require_agent().brain.llm_model, catalog)
    entries = (
        [(step, 1) for step in turn.steps if step.input_tokens > 0 or step.output_tokens > 0]
        if turn.steps
        else [(usage, max(turn.num_turns, 1))]
    )
    try:
        for index, (step_usage, calls) in enumerate(entries):
            fields = UsageFields(
                tok_in=step_usage.input_tokens,
                tok_out=step_usage.output_tokens,
                tok_cached=step_usage.cache_read_input_tokens or None,
                cache_write_5m=step_usage.cache_creation_input_tokens,
                source=ctx.harness.value,
                for_agent_id=agent_id,
                calls=calls,
                unpriced_already_reported=index > 0,
            )
            log_usage_fields(
                pricing_model,
                fields,
                usage_kind="agent",
                catalog=catalog,
                now=ctx.require_clock().now(),
            )
    except Exception as error:
        # meiva:note
        # A bill that cannot be priced is a hole in an accounting row; the reply it
        # describes is real either way. The turn has already finished by the time it
        # is logged, so a raise here deletes a reply the user paid for — a provider
        # that reports token counts in a shape the price book rejects must not cost
        # the answer, and the recorded warning keeps the gap visible.
        # meiva:link agent/graph/docs/harness-turn.md
        logger.warning(
            "[{label}] {body}",
            label="harness",
            event="harness_usage_log_failed",
            body=(
                f"agent {agent_id}'s {ctx.harness.value} turn finished but its usage could not be "
                f"priced: {error!r}"
            ),
            agent_id=agent_id,
            tok_in=usage.input_tokens,
            tok_out=usage.output_tokens,
            tok_cached=usage.cache_read_input_tokens,
            cache_write=usage.cache_creation_input_tokens,
        )


@dataclass(frozen=True, slots=True)
class _OpenTurnTiming:
    turn_id: str
    started_at: datetime
    started_iso: str
    start_offset: int


# meiva:note
# Tracks per-Turn active timing across a single harness foreground run: opens an active marker in
# TurnTimingStore and emits `TurnStarted`, attributes live projected items before Turn end, and
# appends the settled duration span plus closing `TimelineSnapshot` at `finish`.
class HarnessTurnTimingTracker:
    def __init__(
        self,
        ctx: MeivaContext,
        agent_id: int,
        publisher: AgentEventPublisher,
        *,
        session_id: Callable[[], str | None],
        initial_end: int,
    ) -> None:
        self._ctx = ctx
        self._agent_id = agent_id
        self._harness = ctx.harness.value
        self._publisher = publisher
        self._session_id = session_id
        self._store = (
            TurnTimingStore(harness_sessions_dir(home=ctx.meiva_home))
            if ctx.meiva_home is not None
            else None
        )
        self._open_turn: _OpenTurnTiming | None = None
        self._closed_spans: list[TurnTimingSpan] = []
        self._last_closed_end_offset = initial_end
        self._published: dict[str, ProjectedItem] = {}

    @property
    def active_turn_id(self) -> str | None:
        return (
            timeline_turn_id(self._open_turn.turn_id, self._open_turn.started_iso)
            if self._open_turn is not None
            else None
        )

    @property
    def active_turn_started_at(self) -> str | None:
        return self._open_turn.started_iso if self._open_turn is not None else None

    def start_turn(self, turn_id: str, *, start_offset: int) -> None:
        if not turn_id:
            return
        if self._open_turn is not None and self._open_turn.turn_id == turn_id:
            return
        now = self._ctx.require_clock().now()
        started_iso = now.isoformat()
        resolved_start = max(start_offset, self._last_closed_end_offset)
        self._open_turn = _OpenTurnTiming(
            turn_id=turn_id,
            started_at=now,
            started_iso=started_iso,
            start_offset=resolved_start,
        )
        self._published.clear()
        if self._store is not None:
            self._store.open_turn(
                self._agent_id,
                self._harness,
                turn_id=turn_id,
                started_at=started_iso,
                start_offset=resolved_start,
                session_id=self._session_id(),
            )
        self._publisher.emit(
            ProjectionTurnStarted(
                agent_id=self._agent_id,
                turn_id=timeline_turn_id(turn_id, started_iso),
                started_at=started_iso,
            ).model_dump_json()
        )

    def clear_open_turn(self) -> None:
        self._open_turn = None
        self._published.clear()
        if self._store is not None:
            self._store.clear_active(self._agent_id, self._harness)

    def _active_spans(self) -> list[TurnTimingSpan]:
        spans = list(self._closed_spans)
        if self._open_turn is not None:
            spans.append(
                TurnTimingSpan(
                    turn_id=timeline_turn_id(self._open_turn.turn_id, self._open_turn.started_iso),
                    start_pos=self._open_turn.start_offset,
                    end_pos=None,
                    duration_ms=None,
                )
            )
        return spans

    def apply(self, items: Sequence[ProjectedItem]) -> None:
        spans = self._active_spans()
        if spans:
            apply_turn_timings([item.item for item in items], spans)
        if self._open_turn is not None:
            for item in items:
                self._published[item.item.item_id] = item

    def refresh(self, items: Sequence[ProjectedItem]) -> None:
        spans = self._active_spans()
        if spans:
            apply_turn_timings([item.item for item in items], spans)
        for item in items:
            if item.item.item_id in self._published:
                self._published[item.item.item_id] = item

    def close_turn(
        self,
        *,
        turn_id: str | None,
        end_offset: int,
        ended_at: datetime,
        fallback_duration_ms: int,
    ) -> None:
        open_turn, self._open_turn = self._open_turn, None
        tid = turn_id or (open_turn.turn_id if open_turn is not None else None)
        min_item_offset = min(
            (p.offset for p in self._published.values()),
            default=open_turn.start_offset if open_turn is not None else end_offset,
        )
        if open_turn is not None:
            duration_ms = max(0, round((ended_at - open_turn.started_at).total_seconds() * 1000))
            started_iso = open_turn.started_iso
            base_start = min(open_turn.start_offset, min_item_offset)
        else:
            duration_ms = max(0, fallback_duration_ms)
            started_iso = ended_at.isoformat()
            base_start = min_item_offset
        start_offset = max(self._last_closed_end_offset, base_start)
        resolved_end = max(end_offset, start_offset)
        timing_id: str | None = None
        if tid:
            timing_id = timeline_turn_id(tid, started_iso)
            span = TurnTimingSpan(timing_id, start_offset, resolved_end, duration_ms)
            self._closed_spans.append(span)
            if self._store is not None:
                self._store.close_turn(
                    self._agent_id,
                    self._harness,
                    session_id=self._session_id(),
                    turn_id=tid,
                    start_offset=start_offset,
                    end_offset=resolved_end,
                    started_at=started_iso,
                    ended_at=ended_at.isoformat(),
                    duration_ms=duration_ms,
                )
        elif self._store is not None:
            self._store.clear_active(self._agent_id, self._harness)

        self._last_closed_end_offset = max(self._last_closed_end_offset, resolved_end)
        published_items = [p.item for p in self._published.values()]
        self._published.clear()
        if published_items and timing_id:
            apply_turn_timings(
                published_items,
                (TurnTimingSpan(timing_id, start_offset, resolved_end, duration_ms),),
            )
            self._publisher.emit(
                TimelineSnapshot(
                    agent_id=self._agent_id,
                    items=[item.model_dump() for item in published_items],
                    msg_count=resolved_end,
                    active_turn_id=None,
                    active_turn_started_at=None,
                ).model_dump_json()
            )

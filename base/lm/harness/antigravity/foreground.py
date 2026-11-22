from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from itertools import combinations

from base.host.boundary.json_values import is_object_dict
from base.lm.harness.antigravity.acp import ToolCall, input_marker, received_markers
from base.lm.harness.antigravity.mapping import (
    OutputProjection,
    PlannerRow,
    has_planner_tokens,
    planner_rows,
    planner_step_usage,
    planner_tokens,
)
from base.lm.harness.antigravity.subagents import INVOKE_SUBAGENT
from base.lm.harness.base_session import ActiveTurn
from base.lm.harness.events import (
    AssistantText,
    ReasoningDelta,
    ReasoningText,
    TextDelta,
    ToolResult,
    ToolUse,
    TurnCompleted,
    Usage,
)
from base.log import logger


def _tokens(value: object) -> int:
    return value if type(value) is int and value >= 0 else 0


@dataclass(frozen=True, slots=True)
class PromptUsage:
    input: int
    output: int
    cached_read: int
    cached_write: int
    thought: int

    @classmethod
    def from_result(cls, usage: object) -> PromptUsage | None:
        if not is_object_dict(usage) or not all(
            type(usage.get(name)) is int for name in ("inputTokens", "outputTokens")
        ):
            return None
        return cls(
            _tokens(usage.get("inputTokens")),
            _tokens(usage.get("outputTokens")),
            _tokens(usage.get("cachedReadTokens")),
            _tokens(usage.get("cachedWriteTokens")),
            _tokens(usage.get("thoughtTokens")),
        )

    def billed(self) -> Usage:
        return Usage(
            self.input + self.cached_read + self.cached_write,
            self.output + self.thought,
            self.cached_write,
            self.cached_read,
        )


def planner_usage(
    rows: Sequence[dict[str, object]], keys: Sequence[str], final_text: str
) -> tuple[Usage, ...]:
    marks = {input_marker(key) for key in keys}
    if received_markers(rows) & marks != marks:
        return ()
    planner = [
        step.row
        for step in planner_rows(rows, keys)
        if step.input_key is not None and step.notice is None
    ]
    if not planner or not final_text:
        return ()
    last_text = planner[-1].get("content")
    if not isinstance(last_text, str) or last_text.strip() != final_text.strip():
        return ()
    return planner_step_usage(planner)


@dataclass(frozen=True, slots=True)
class TurnBilling:
    usage: Usage
    uncertain: bool = False
    steps: tuple[Usage, ...] = ()
    calls: int = 1


def turn_usage(
    reports: Sequence[PromptUsage],
    steps: Sequence[Usage],
    *,
    request_count: int,
    terminal_reported: bool,
    span_verified: bool = False,
) -> TurnBilling:
    if request_count == 1 and reports:
        return TurnBilling(reports[-1].billed())
    recorded = sum(steps, Usage())
    all_reported = len(reports) == request_count
    if steps and terminal_reported:
        if (all_reported or span_verified) and _covers_cumulative(reports, steps, recorded):
            return TurnBilling(reports[-1].billed())
        if all_reported and _covers_disjoint(reports, steps):
            return TurnBilling(sum((report.billed() for report in reports), Usage()))
    if terminal_reported or not steps:
        return TurnBilling(reports[-1].billed() if reports else Usage(), uncertain=True)
    return TurnBilling(recorded + _matched_extras(reports, steps), uncertain=True)


def _covers_cumulative(
    reports: Sequence[PromptUsage], steps: Sequence[Usage], recorded: Usage
) -> bool:
    return (
        bool(reports)
        and _visible(reports[-1]) == recorded
        and all(
            _end_of_span(_visible(report), steps[:-1], 0) is not None for report in reports[:-1]
        )
    )


def _covers_disjoint(reports: Sequence[PromptUsage], steps: Sequence[Usage]) -> bool:
    cursor = 0
    for report in reports:
        end = _end_of_span(_visible(report), steps, cursor)
        if end is None:
            return False
        cursor = end
    return cursor == len(steps)


def _end_of_span(target: Usage, steps: Sequence[Usage], start: int) -> int | None:
    running = Usage()
    for index in range(start, len(steps)):
        running += steps[index]
        if running == target:
            return index + 1
    return None


def _visible(report: PromptUsage) -> Usage:
    return Usage(
        report.input + report.cached_read, report.output, cache_read_input_tokens=report.cached_read
    )


def _matched_extras(reports: Sequence[PromptUsage], steps: Sequence[Usage]) -> Usage:
    cursor = 0
    extras = Usage()
    for report in reports:
        match = _match_usage(_visible(report), steps, cursor)
        if match is not None:
            cursor = match
            extras += Usage(report.cached_write, report.thought, report.cached_write)
    return extras


def _match_usage(target: Usage, steps: Sequence[Usage], cursor: int) -> int | None:
    for start in range(cursor, len(steps)):
        running = Usage()
        for end in range(start, len(steps)):
            running += steps[end]
            if running == target:
                return end + 1
            if running.input_tokens > target.input_tokens:
                break
    return None


def bill_prompt(
    rows: Sequence[dict[str, object]],
    keys: Sequence[str],
    final_text: str,
    reports: Mapping[int, PromptUsage],
    latest: int,
    *,
    verified: tuple[Usage, ...] | None = None,
) -> TurnBilling:
    steps = planner_usage(rows, keys, final_text) if verified is None else verified
    if verified and len(keys) == 1 and not reports:
        return TurnBilling(sum(verified, Usage()), calls=len(verified), steps=verified)
    bill = turn_usage(
        [reports[request] for request in sorted(reports)],
        steps,
        request_count=len(keys),
        terminal_reported=latest in reports,
        span_verified=verified is not None,
    )
    if bill.uncertain:
        return bill
    return replace(
        bill, calls=max(1, len(steps)), steps=steps if sum(steps, Usage()) == bill.usage else ()
    )


MAX_AMBIGUOUS_PLANNER_ROWS = 10


def _witnesses(
    known: tuple[PlannerRow, ...],
    possible: tuple[PlannerRow, ...],
    reports: Sequence[PromptUsage],
    keys: Sequence[str],
) -> Iterator[tuple[PlannerRow, ...]]:
    if not reports or not all(has_planner_tokens(step.row) for step in (*known, *possible)):
        return
    if (span := _sole_carried_span(known, possible, reports, keys)) is not None:
        yield span
        return
    if len(possible) > MAX_AMBIGUOUS_PLANNER_ROWS:
        return
    terminals = {step.index for step in possible if step.final}
    for count in range(len(possible) + 1):
        for extra in combinations(possible, count):
            if extra and not terminals.difference(step.index for step in extra):
                continue
            span = tuple(sorted((*known, *extra), key=lambda step: step.index))
            if _matches_reports(span, reports, keys):
                yield span


def _sole_carried_span(
    known: tuple[PlannerRow, ...],
    possible: tuple[PlannerRow, ...],
    reports: Sequence[PromptUsage],
    keys: Sequence[str],
) -> tuple[PlannerRow, ...] | None:
    if not possible or not all(step.carries_notice for step in possible):
        return None
    span = tuple(sorted((*known, *possible), key=lambda step: step.index))
    if sum(step.terminal for step in span) == 1 and _matches_reports(span, reports, keys):
        return span
    return None


def _matches_reports(
    span: Sequence[PlannerRow], reports: Sequence[PromptUsage], keys: Sequence[str]
) -> bool:
    if not span or not span[-1].terminal or span[-1].input_key != keys[-1]:
        return False
    steps = tuple(planner_tokens(step.row) for step in span)
    cumulative = _covers_cumulative(reports, steps, sum(steps, Usage()))
    if len(reports) == len(keys):
        return cumulative or _covers_disjoint(reports, steps)
    return cumulative and set(keys).issubset(step.input_key for step in span)


def _planner_text(steps: Iterable[PlannerRow]) -> Iterator[tuple[PlannerRow, str]]:
    for step in steps:
        value = step.row.get("content")
        if isinstance(value, str) and (text := value.strip()):
            yield step, text


@dataclass(slots=True)
class PromptTurn:
    ticket: ActiveTurn
    transcript_offset: int
    stop_reason: str | None = None
    stop_deadline: float = 0.0
    keys: list[str] = field(default_factory=list[str])
    requests: dict[int, str] = field(default_factory=dict[int, str])
    latest: int = 0
    reports: dict[int, PromptUsage] = field(default_factory=dict[int, PromptUsage])
    finished: asyncio.Event = field(default_factory=asyncio.Event)
    interrupted: bool = False
    text: list[str] = field(default_factory=list[str])
    verified_steps: tuple[Usage, ...] | None = None
    _verified_text: tuple[str, ...] = ()
    _verified_usage_partial: bool = False
    _projection_degraded: bool = False

    def record_text(self, text: str) -> Iterator[ReasoningText | TextDelta]:
        yield from self._flush_thoughts()
        self.text.append(text)
        yield TextDelta(self.ticket.turn_id, text)

    def record_thought(self, text: str) -> ReasoningDelta:
        self.ticket.thoughts.append(text)
        return ReasoningDelta(self.ticket.turn_id, text)

    def flush_text(self) -> Iterator[ReasoningText | AssistantText]:
        yield from self._flush_thoughts()
        text = "".join(self.text).strip()
        self.text.clear()
        if text:
            self.ticket.parts.append(text)
            yield AssistantText(self.ticket.turn_id, text)

    def _flush_thoughts(self) -> Iterator[ReasoningText]:
        text = "".join(self.ticket.thoughts).strip()
        self.ticket.thoughts.clear()
        if text:
            yield ReasoningText(self.ticket.turn_id, text)

    def register_request(self, request: int, key: str) -> None:
        self.keys.append(key)
        self.requests[request] = key
        self.latest = request
        self.stop_reason = None
        self.verified_steps = None
        self._verified_text = ()
        self._verified_usage_partial = False
        self._projection_degraded = False

    def require_projection(self, *, required: bool) -> OutputProjection | None:
        return OutputProjection(self.transcript_offset, fences_replay=True) if required else None

    def prove_projection(
        self, projection: OutputProjection, rows: Sequence[dict[str, object]]
    ) -> None:
        if self.stop_reason is None or self.verified_steps is not None:
            return
        scope = tuple(step for step in projection.steps(rows) if step.input_key in self.keys)
        known = tuple(step for step in scope if projection.owns(step))
        possible = tuple(step for step in scope if not projection.owns(step))
        proof = self._confirmed_projection(known, possible, projection)
        if proof is None:
            return
        span, excluded = proof
        self._apply_projection(projection, span, excluded, scope_size=len(scope))

    def _apply_projection(
        self,
        projection: OutputProjection,
        span: tuple[PlannerRow, ...],
        excluded: tuple[PlannerRow, ...],
        *,
        scope_size: int,
    ) -> None:
        projection.confirm_span(span, excluded)
        self.verified_steps = tuple(planner_tokens(step.row) for step in span)
        self._verified_text = tuple(text for _, text in _planner_text(span))
        self._verified_usage_partial = len(span) + len(excluded) < scope_size or not all(
            has_planner_tokens(step.row) for step in span
        )

    def _confirmed_projection(
        self,
        known: tuple[PlannerRow, ...],
        possible: tuple[PlannerRow, ...],
        projection: OutputProjection,
    ) -> tuple[tuple[PlannerRow, ...], tuple[PlannerRow, ...]] | None:
        if self.stop_reason == "cancelled":
            return known, ()
        reports = self._projection_reports()
        if (
            known
            and self._proves_end(known[-1])
            and (not reports or (projection.fences_replay and not possible))
        ):
            return known, tuple(step for step in possible if step.index > known[-1].index)
        witnesses = _witnesses(known, possible, reports, self.keys)
        span = next(witnesses, None)
        if span is None or next(witnesses, None) is not None:
            return None
        chosen = {step.index for step in span}
        return span, tuple(step for step in possible if step.index not in chosen)

    def _proves_end(self, step: PlannerRow) -> bool:
        report = self.reports.get(self.latest)
        return step.input_key == self.keys[-1] and (
            step.terminal
            or (
                self.stop_reason == "end_turn"
                and step.blank_reply
                and report is not None
                and report.output == 0
            )
        )

    def _projection_reports(self) -> tuple[PromptUsage, ...]:
        if self.latest not in self.reports:
            return ()
        return tuple(self.reports[request] for request in sorted(self.reports))

    def projection_ready(
        self, projection: OutputProjection, rows: Sequence[dict[str, object]]
    ) -> bool:
        if self.verified_steps is not None:
            return True
        if self.stop_reason == "end_turn" and not self._latest_marker_recorded(rows):
            return False
        return self._projection_frames_ready(projection, rows)

    def _projection_frames_ready(
        self, projection: OutputProjection, rows: Sequence[dict[str, object]]
    ) -> bool:
        scope = [step for step in projection.steps(rows) if step.input_key in self.keys]
        if any(not projection.owns(step) for step in scope):
            return False
        if scope and scope[-1].final:
            return (len(self.reports) < len(self.keys) or self.latest not in self.reports) and (
                scope[-1].index,
                "content",
                0,
            ) in projection.seen
        return not projection.pending and (
            bool(scope) or not any(report.output for report in self._projection_reports())
        )

    def _latest_marker_recorded(self, rows: Sequence[dict[str, object]]) -> bool:
        return input_marker(self.keys[-1]) in received_markers(rows[self.transcript_offset :])

    def settle_projection(
        self,
        projection: OutputProjection,
        rows: Sequence[dict[str, object]],
        monotonic: Callable[[], float],
    ) -> str | None:
        reason = self.stop_reason
        if reason is None or self.projection_ready(projection, rows):
            return reason
        if reason != "end_turn" or monotonic() < self.stop_deadline:
            return None
        if not self._latest_marker_recorded(rows):
            return None
        scope = tuple(step for step in projection.steps(rows) if step.input_key in self.keys)
        owned = tuple(step for step in scope if projection.owns(step))
        self._apply_projection(projection, owned, (), scope_size=len(scope))
        self._projection_degraded = True
        logger.warning(
            "Settling completed agy ACP prompt with only attributed output",
            event="agy_acp_attribution_degraded",
            turn_id=self.ticket.turn_id,
            unresolved_rows=len(scope) - len(owned),
            pending_frames=len(projection.pending),
        )
        return reason

    def close_projection(self, projection: OutputProjection, row_count: int) -> None:
        projection.pending.clear()
        projection.close_foreground(
            self.keys,
            row_count,
            accounted=self.verified_steps is not None and not self._projection_degraded,
            cancelled=self.interrupted,
            degraded=self._projection_degraded,
        )

    def restore_verified_text(
        self, projection: OutputProjection, rows: Sequence[dict[str, object]]
    ) -> Iterator[str]:
        if self.verified_steps is None:
            return
        steps = (
            step
            for step in projection.steps(rows)
            if step.index in projection.confirmed and step.input_key in self.keys
        )
        for step, text in _planner_text(steps):
            evidence = (step.index, "content", 0)
            if evidence not in projection.seen:
                projection.seen.add(evidence)
                yield text

    def complete(
        self,
        rows: list[dict[str, object]],
        reason: str,
        session_id: str | None,
        monotonic_now: float,
        generation: int,
    ) -> TurnCompleted:
        ticket = self.ticket
        parts = self._verified_text or ticket.parts
        final = parts[-1] if parts else ""
        bill = bill_prompt(
            rows[self.transcript_offset :],
            self.keys,
            final,
            self.reports,
            self.latest,
            verified=self.verified_steps,
        )
        interrupted = reason == "cancelled"
        subtype = "interrupted" if interrupted else "denied" if ticket.denials else "success"
        return TurnCompleted(
            turn_id=ticket.turn_id,
            session_id=session_id,
            text="\n".join(parts),
            subtype=subtype,
            is_error=reason != "end_turn" or bool(ticket.denials),
            interrupted=interrupted,
            stop_reason=reason,
            num_turns=bill.calls,
            duration_ms=int((monotonic_now - (ticket.started_at or 0)) * 1000),
            usage=bill.usage,
            usage_uncertain=bill.uncertain
            or self._projection_degraded
            or (self._verified_usage_partial and self.latest not in self.reports),
            cost_usd=None,
            cumulative_cost_usd=None,
            permission_denials=tuple(ticket.denials),
            steps=bill.steps,
            generation=generation,
        )


@dataclass(slots=True)
class ToolUpdates:
    calls: dict[str, ToolCall] = field(default_factory=dict[str, ToolCall])

    def __contains__(self, tool_id: str) -> bool:
        return tool_id in self.calls

    def clear(self) -> None:
        self.calls.clear()

    def update(
        self, frame: dict[str, object], turn_id: str | None
    ) -> Iterator[ToolUse | ToolResult]:
        tool_id = frame.get("toolCallId")
        if not isinstance(tool_id, str) or frame.get("name") == INVOKE_SUBAGENT:
            return
        call = self.calls.get(tool_id)
        if call is None:
            if turn_id is None or frame.get("sessionUpdate") != "tool_call":
                return
            call = ToolCall.from_update(frame, turn_id=turn_id, tool_id=tool_id)
            self.calls[tool_id] = call
            yield call.use
        if (result := call.result(frame)) is not None:
            del self.calls[tool_id]
            yield result

    def cancel(self, turn_id: str) -> Iterator[ToolResult]:
        for tool_id, call in tuple(self.calls.items()):
            if call.use.turn_id == turn_id:
                del self.calls[tool_id]
                yield ToolResult(turn_id, tool_id, is_error=True, content="Cancelled")

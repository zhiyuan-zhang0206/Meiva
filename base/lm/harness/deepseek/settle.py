# meiva:note
# What a DeepSeek session makes of what the runtime sends back. Reading a notification, deciding
# which answer settles a turn, and folding the turn's usage and cost into its report are decisions
# over values; the session keeps the awaits, the queue and the state it assigns them to.
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from base.lm.harness.acp import AcpNotification
from base.lm.harness.deepseek.mapping import assistant_text, parse_prompt_result
from base.lm.harness.deepseek.usage import UsageSnapshot
from base.lm.harness.events import AssistantText, SessionFaulted, TurnCompleted, Usage
from base.lm.harness.turn import SessionTotals, TurnOutcome
from base.lm.pricing import PriceBook, quote

PromptResult = Mapping[str, object]
UpdateMap = Mapping[str, object]

SESSION_UPDATE = "session/update"
USAGE_UPDATE = "usage_update"
SURVIVOR_FAULT = "{count} descendant(s) survived {reason}; inspect before reuse"


@dataclass(frozen=True, slots=True)
class ContextOccupancy:
    used: int | None
    size: int | None


@dataclass(frozen=True, slots=True)
class TurnUpdate:
    update: UpdateMap


# meiva:note
# A `session/update` that arrived with no turn in flight: the runtime's own work.
@dataclass(frozen=True, slots=True)
class UnclaimedUpdate: ...


# meiva:note
# Another method, another session's update, or an update of a shape this bridge does not read.
@dataclass(frozen=True, slots=True)
class IgnoredNotification: ...


type Route = ContextOccupancy | TurnUpdate | UnclaimedUpdate | IgnoredNotification


# meiva:note
# `session/update` carries no turn id, so an update that arrives with nothing in flight cannot be
# attributed: it is the runtime's own work (a background command, a sub-session) and is counted
# rather than carried into the next turn (#5664). Mid-turn the two are indistinguishable; that is
# the adapter's documented residual risk.
def route_notification(
    notification: AcpNotification, *, session_id: str | None, turn_in_flight: bool
) -> Route:
    if notification.method != SESSION_UPDATE:
        return IgnoredNotification()
    params = notification.params
    if params.get("sessionId") != session_id:
        return IgnoredNotification()
    raw_update: UpdateMap | None = params.get("update")
    if not isinstance(raw_update, dict):
        return IgnoredNotification()
    if raw_update.get("sessionUpdate") == USAGE_UPDATE:
        used, size = raw_update.get("used"), raw_update.get("size")
        return ContextOccupancy(
            used=used if isinstance(used, int) else None,
            size=size if isinstance(size, int) else None,
        )
    if not turn_in_flight:
        return UnclaimedUpdate()
    return TurnUpdate(update=raw_update)


# meiva:note
# The latest turn's tokens, and whether the log could measure them at all.
def summarize_turn_usage(observed: UsageSnapshot) -> tuple[Usage, bool]:
    if not observed.per_turn_usage:
        return (Usage(), False)
    steps = observed.latest_turn()
    return (
        Usage(
            input_tokens=sum(step.input_tokens for step in steps),
            output_tokens=sum(step.output_tokens for step in steps),
            cache_creation_input_tokens=sum(step.cache_write_tokens for step in steps),
            cache_read_input_tokens=sum(step.cache_read_tokens for step in steps),
        ),
        True,
    )


def turn_cost(*, model: str | None, usage: Usage, prices: PriceBook | None) -> float | None:
    if prices is None or model is None:
        return None
    quoted = quote(
        model,
        usage.input_tokens + usage.cache_read_input_tokens + usage.cache_creation_input_tokens,
        usage.output_tokens,
        usage.cache_read_input_tokens,
        prices=prices,
    )
    return None if quoted is None else quoted.cost_usd


def survivor_fault(count: int, *, reason: str) -> SessionFaulted:
    return SessionFaulted(turn_id=None, error=SURVIVOR_FAULT.format(count=count, reason=reason))


# meiva:note
# The turn being settled: what it answered, and what the session saw of how it ran.
@dataclass(frozen=True, slots=True)
class TurnSubject:
    turn_id: str
    session_id: str | None
    parts: Sequence[str]
    result: PromptResult
    interrupt_requested: bool
    permission_denials: Sequence[str]
    started_at: float | None


# meiva:note
# What the turn is counted and priced with.
@dataclass(frozen=True, slots=True)
class TurnMeasurement:
    observed: UsageSnapshot | None
    model: str | None
    prices: PriceBook | None
    totals: SessionTotals


# meiva:note
# One settled turn: the report the caller emits, and the totals it carries forward.
@dataclass(frozen=True, slots=True)
class SettledTurn:
    outcome: TurnOutcome
    completed: TurnCompleted
    assistant: AssistantText | None
    totals: SessionTotals


# meiva:note
# The answer text is the turn's streamed parts; a runtime answer that carries none falls back to
# what the prompt result itself holds, which is the adapter's only view of a turn it did not read.
def settle_turn(
    subject: TurnSubject, measurement: TurnMeasurement, *, now_monotonic: float
) -> SettledTurn:
    fields = parse_prompt_result(subject.result)
    text = "\n".join(subject.parts).strip()
    if measurement.observed is None:
        usage, measured = (Usage(), False)
    else:
        usage, measured = summarize_turn_usage(measurement.observed)
    cost = (
        turn_cost(model=measurement.model, usage=usage, prices=measurement.prices)
        if measured
        else None
    )
    totals = measurement.totals.with_turn(usage, measured=measured, cost=cost)
    outcome = TurnOutcome(
        turn_id=subject.turn_id,
        text=text,
        subtype=fields.subtype,
        is_error=fields.is_error,
        interrupted=subject.interrupt_requested or fields.interrupted,
        stop_reason=fields.stop_reason,
        usage=usage,
        cost_usd=cost,
        permission_denials=tuple(subject.permission_denials),
        per_turn_usage=measured,
    )
    started = subject.started_at if subject.started_at else now_monotonic
    completed = TurnCompleted(
        turn_id=subject.turn_id,
        session_id=subject.session_id,
        text=text,
        subtype=outcome.subtype,
        is_error=outcome.is_error,
        interrupted=outcome.interrupted,
        stop_reason=outcome.stop_reason,
        num_turns=len(subject.parts),
        duration_ms=int((now_monotonic - started) * 1000),
        usage=usage,
        cost_usd=cost,
        cumulative_cost_usd=totals.cost_usd if totals.cost_known else None,
        permission_denials=outcome.permission_denials,
    )
    return SettledTurn(
        outcome=outcome,
        completed=completed,
        assistant=assistant_text(text, turn_id=subject.turn_id),
        totals=totals,
    )

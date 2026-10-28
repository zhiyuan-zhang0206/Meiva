from __future__ import annotations

from dataclasses import dataclass

from base.lm.harness.claude.mapping import ResultFields
from base.lm.harness.events import TurnCompleted, UnsolicitedOutput
from base.lm.harness.turn import SessionTotals, TurnOutcome

# meiva:note
# How one result message settles a turn: what the outcome is, what the stream is told, and what
# the session has spent afterwards. Pure, so the two shapes the base session consumes cannot
# disagree about the text, the step count or the running total.
# meiva:link base/lm/harness/claude/docs/claude.meiva.okf.md


# meiva:note
# What the running turn collected before its result arrived: the text it streamed, whether an
# interrupt was asked for, and how many model steps the CLI reported.
@dataclass(frozen=True, slots=True)
class RunningTurn:
    turn_id: str
    parts: tuple[str, ...]
    interrupt_requested: bool
    steps: int


@dataclass(frozen=True, slots=True)
class ResultSettlement:
    totals: SessionTotals
    session_id: str | None
    completed: TurnCompleted
    outcome: TurnOutcome


# meiva:note
# A terminal message for a turn this session did not run: the CLI's own answer to a background
# task, or a result from a turn it began for some other reason. It is neither this session's
# boundary nor a failure. It is recorded so the stream keeps one entry per turn the provider
# ran, and it is not delivered as new inbound content — the agent already read the reply in the
# provider's transcript, unlike the report that asked for it (#5663).
# meiva:link base/lm/harness/claude/docs/claude.meiva.okf.md
def result_output(fields: ResultFields) -> UnsolicitedOutput:
    return UnsolicitedOutput(source=fields.origin_kind or "result", text=fields.result_text or "")


# meiva:note
# The turn's text is what the assistant streamed, falling back to the result's own text when it
# streamed none; `num_turns` is the larger of the CLI's count and the steps this session saw,
# never below one; and the session id a result names wins over the one the init reported.
# meiva:link base/lm/harness/claude/docs/claude.meiva.okf.md
def settle_result(
    fields: ResultFields,
    *,
    turn: RunningTurn,
    session_id: str | None,
    totals: SessionTotals,
) -> ResultSettlement:
    text = "\n".join(turn.parts).strip() or (fields.result_text or "")
    resolved_session = fields.session_id or session_id
    updated = totals.with_turn(fields.usage, fields.cost_usd)
    outcome = TurnOutcome(
        turn_id=turn.turn_id,
        text=text,
        subtype=fields.subtype,
        is_error=fields.is_error,
        interrupted=turn.interrupt_requested,
        stop_reason=fields.stop_reason,
        usage=fields.usage,
        cost_usd=fields.cost_usd,
        permission_denials=fields.permission_denials,
    )
    completed = TurnCompleted(
        turn_id=turn.turn_id,
        session_id=resolved_session,
        text=text,
        subtype=fields.subtype,
        is_error=fields.is_error,
        interrupted=turn.interrupt_requested,
        stop_reason=fields.stop_reason,
        num_turns=max(fields.num_turns, turn.steps, 1),
        duration_ms=fields.duration_ms,
        usage=fields.usage,
        cost_usd=fields.cost_usd,
        cumulative_cost_usd=updated.cost_usd,
        permission_denials=fields.permission_denials,
    )
    return ResultSettlement(
        totals=updated, session_id=resolved_session, completed=completed, outcome=outcome
    )

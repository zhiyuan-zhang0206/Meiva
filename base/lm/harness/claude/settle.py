from __future__ import annotations

from dataclasses import dataclass

from base.lm.harness.claude.mapping import AssistantError, ResultFields, sanitize_error_text
from base.lm.harness.events import TurnCompleted
from base.lm.harness.session import SessionTotals

# meiva:note
# How one result message settles a turn: what the outcome is, what the stream is told, and what
# the session has spent afterwards. Pure, so the two shapes the base session consumes cannot
# disagree about the text, the step count or the running total.
# meiva:link base/lm/harness/claude/docs/claude.md


# meiva:note
# What the running turn collected before its result arrived: streamed text, model steps, and
# any API-error assistant frames emitted before the trailing result.
@dataclass(frozen=True, slots=True)
class RunningTurn:
    turn_id: str
    parts: tuple[str, ...]
    steps: int
    assistant_errors: tuple[AssistantError, ...] = ()


@dataclass(frozen=True, slots=True)
class ResultSettlement:
    totals: SessionTotals
    session_id: str | None
    completed: TurnCompleted


# meiva:note
# The turn's text is what the assistant streamed, falling back to the result's own text when it
# streamed none; `num_turns` is the larger of the CLI's count and the steps this session saw,
# never below one; and the session id a result names wins over the one the init reported.
# meiva:link base/lm/harness/claude/docs/claude.md
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
    is_error = fields.is_error or (bool(turn.assistant_errors) and not fields.interrupted)
    api_error_status, error_code, errors = _resolve_errors(fields, turn, is_error=is_error)
    completed = TurnCompleted(
        turn_id=turn.turn_id,
        session_id=resolved_session,
        text=text,
        subtype=fields.subtype,
        is_error=is_error,
        interrupted=fields.interrupted,
        stop_reason=fields.stop_reason,
        num_turns=max(fields.num_turns, turn.steps, 1),
        duration_ms=fields.duration_ms,
        usage=fields.usage,
        cost_usd=fields.cost_usd,
        cumulative_cost_usd=updated.cost_usd,
        permission_denials=fields.permission_denials,
        api_error_status=api_error_status,
        errors=errors,
        error_code=error_code,
    )
    return ResultSettlement(totals=updated, session_id=resolved_session, completed=completed)


def _resolve_errors(
    fields: ResultFields, turn: RunningTurn, *, is_error: bool
) -> tuple[int | None, str | None, tuple[str, ...]]:
    api_error_status = fields.api_error_status
    if not is_error or not turn.assistant_errors:
        return api_error_status, None, fields.errors
    if api_error_status is None:
        api_error_status = next(
            (e.api_error_status for e in turn.assistant_errors if e.api_error_status is not None),
            None,
        )
    error_code = next((e.error for e in reversed(turn.assistant_errors) if e.error), None)
    errors = fields.errors or _assistant_error_details(turn.assistant_errors, fields.result_text)
    return api_error_status, error_code, errors


def _assistant_error_details(
    assistant_errors: tuple[AssistantError, ...], result_text: str | None
) -> tuple[str, ...]:
    texts = tuple(dict.fromkeys(e.text for e in assistant_errors if e.text))
    if texts:
        return texts
    if result_text and (sanitized := sanitize_error_text(result_text)):
        return (sanitized,)
    return ()

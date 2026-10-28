from __future__ import annotations

from dataclasses import replace

from base.lm.harness.claude.mapping import ResultFields
from base.lm.harness.claude.settle import (
    RunningTurn,
    result_output,
    settle_result,
)
from base.lm.harness.events import UnsolicitedOutput, Usage
from base.lm.harness.turn import SessionTotals

USAGE = Usage(
    input_tokens=4,
    output_tokens=138,
    cache_creation_input_tokens=0,
    cache_read_input_tokens=2738,
)


REPORTED = ResultFields(
    session_id="sess-result",
    subtype="success",
    is_error=False,
    stop_reason="end_turn",
    num_turns=1,
    duration_ms=1200,
    usage=USAGE,
    cost_usd=0.25,
    result_text="the result's own text",
    permission_denials=("Bash",),
    origin_kind="human",
)


def fields(**overrides: object) -> ResultFields:
    return replace(REPORTED, **overrides)


def running(
    parts: tuple[str, ...] = ("one", "two"), *, steps: int = 1, interrupted: bool = False
) -> RunningTurn:
    return RunningTurn(turn_id="turn-7", parts=parts, interrupt_requested=interrupted, steps=steps)


def test_the_settled_text_is_what_the_turn_streamed() -> None:
    settlement = settle_result(
        fields(), turn=running(), session_id="sess-init", totals=SessionTotals()
    )

    assert settlement.outcome.text == "one\ntwo"
    assert settlement.completed.text == "one\ntwo"
    assert settlement.outcome.turn_id == settlement.completed.turn_id == "turn-7"


def test_a_turn_that_streamed_nothing_settles_on_the_results_text() -> None:
    settlement = settle_result(
        fields(), turn=running(("  ", " ")), session_id="sess-init", totals=SessionTotals()
    )

    assert settlement.outcome.text == "the result's own text"


def test_the_step_count_is_the_larger_of_the_cli_count_and_what_the_session_saw() -> None:
    def steps(claimed: int, seen: int) -> int:
        settlement = settle_result(
            fields(num_turns=claimed),
            turn=running(steps=seen),
            session_id=None,
            totals=SessionTotals(),
        )
        return settlement.completed.num_turns

    assert steps(claimed=1, seen=3) == 3
    assert steps(claimed=5, seen=2) == 5
    assert steps(claimed=0, seen=0) == 1


def test_the_totals_carry_the_turn_into_the_settlement() -> None:
    settlement = settle_result(
        fields(),
        turn=running(),
        session_id="sess-init",
        totals=SessionTotals(input_tokens=10, output_tokens=1, cost_usd=0.5, turns=1),
    )

    assert settlement.totals.input_tokens == 14
    assert settlement.totals.output_tokens == 139
    assert settlement.totals.cache_read_input_tokens == 2738
    assert settlement.totals.turns == 2
    assert settlement.totals.cost_usd == 0.75
    assert settlement.completed.cumulative_cost_usd == 0.75
    assert settlement.completed.cost_usd == 0.25


def test_a_result_that_names_a_session_replaces_the_one_the_init_reported() -> None:
    named = settle_result(fields(), turn=running(), session_id="sess-init", totals=SessionTotals())
    anonymous = settle_result(
        fields(session_id=None), turn=running(), session_id="sess-init", totals=SessionTotals()
    )

    assert named.session_id == "sess-result"
    assert named.completed.session_id == "sess-result"
    assert anonymous.session_id == "sess-init"
    assert anonymous.completed.session_id == "sess-init"


def test_the_settled_turn_keeps_the_fields_the_result_reported() -> None:
    settlement = settle_result(
        fields(is_error=True, subtype="error_during_execution", stop_reason=None),
        turn=running(interrupted=True),
        session_id=None,
        totals=SessionTotals(),
    )

    assert settlement.outcome.interrupted is True
    assert settlement.completed.interrupted is True
    assert settlement.outcome.is_error is True
    assert settlement.outcome.subtype == "error_during_execution"
    assert settlement.completed.stop_reason is None
    assert settlement.outcome.permission_denials == ("Bash",)
    assert settlement.completed.duration_ms == 1200
    assert settlement.completed.usage == USAGE


def test_a_result_outside_our_turn_is_labelled_with_its_origin() -> None:
    assert result_output(fields(origin_kind="task-notification", result_text="read me")) == (
        UnsolicitedOutput(source="task-notification", text="read me")
    )
    assert result_output(fields(origin_kind=None, result_text=None)) == UnsolicitedOutput(
        source="result", text=""
    )

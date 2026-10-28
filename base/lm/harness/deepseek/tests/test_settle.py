from __future__ import annotations

from base.lm.harness.acp import AcpNotification
from base.lm.harness.deepseek.settle import (
    ContextOccupancy,
    IgnoredNotification,
    SettledTurn,
    TurnMeasurement,
    TurnSubject,
    TurnUpdate,
    UnclaimedUpdate,
    route_notification,
    settle_turn,
    summarize_turn_usage,
    survivor_fault,
    turn_cost,
)
from base.lm.harness.deepseek.usage import StepUsage, UsageSnapshot
from base.lm.harness.events import Usage
from base.lm.harness.turn import SessionTotals
from base.lm.pricing import PriceBook
from base.lm.pricing.archive import load_archive


def update_notification(params: dict[str, object]) -> AcpNotification:
    return AcpNotification(method="session/update", params=params)


def test_another_method_is_ignored() -> None:
    notification = AcpNotification(method="session/request_permission", params={})
    assert route_notification(notification, session_id="s", turn_in_flight=True) == (
        IgnoredNotification()
    )


def test_another_session_s_update_is_ignored() -> None:
    notification = update_notification({"sessionId": "other", "update": {"sessionUpdate": "x"}})
    assert route_notification(notification, session_id="s", turn_in_flight=True) == (
        IgnoredNotification()
    )


def test_an_update_that_is_not_an_object_is_ignored() -> None:
    notification = update_notification({"sessionId": "s", "update": ["not", "an", "object"]})
    assert route_notification(notification, session_id="s", turn_in_flight=True) == (
        IgnoredNotification()
    )


def test_a_usage_update_reports_the_context_window_it_names() -> None:
    notification = update_notification(
        {"sessionId": "s", "update": {"sessionUpdate": "usage_update", "used": 12, "size": 200}}
    )
    assert route_notification(notification, session_id="s", turn_in_flight=False) == (
        ContextOccupancy(used=12, size=200)
    )


def test_a_usage_update_without_figures_reports_nothing_from_them() -> None:
    notification = update_notification(
        {"sessionId": "s", "update": {"sessionUpdate": "usage_update", "used": "12"}}
    )
    assert route_notification(notification, session_id="s", turn_in_flight=True) == (
        ContextOccupancy(used=None, size=None)
    )


def test_an_update_with_no_turn_in_flight_is_unclaimed() -> None:
    notification = update_notification(
        {"sessionId": "s", "update": {"sessionUpdate": "agent_message_chunk", "content": "x"}}
    )
    assert route_notification(notification, session_id="s", turn_in_flight=False) == (
        UnclaimedUpdate()
    )


def test_an_update_with_a_turn_in_flight_belongs_to_it() -> None:
    update = {"sessionUpdate": "agent_message_chunk", "content": "x"}
    notification = update_notification({"sessionId": "s", "update": update})
    assert route_notification(notification, session_id="s", turn_in_flight=True) == TurnUpdate(
        update=update
    )


def snapshot(*steps: StepUsage, per_turn: bool = True) -> UsageSnapshot:
    return UsageSnapshot(per_turn_usage=per_turn, session_id="session-1", steps=steps)


def test_an_unmeasurable_log_reports_no_usage_and_says_so() -> None:
    observed = snapshot(StepUsage(1, 1, 10, 20, 0, 0), per_turn=False)
    assert summarize_turn_usage(observed) == (Usage(), False)


def test_only_the_latest_turn_is_summarized() -> None:
    observed = snapshot(
        StepUsage(1, 1, 10, 20, 0, 0),
        StepUsage(2, 1, 100, 200, 30, 40),
        StepUsage(2, 2, 1, 2, 3, 4),
    )
    usage, measured = summarize_turn_usage(observed)
    assert measured is True
    assert usage == Usage(
        input_tokens=101,
        output_tokens=202,
        cache_creation_input_tokens=44,
        cache_read_input_tokens=33,
    )


def test_a_turn_is_priced_from_the_book_it_is_given() -> None:
    prices = PriceBook(load_archive(), {})
    usage = Usage(input_tokens=1000, output_tokens=500)
    cost = turn_cost(model="deepseek-v4-flash", usage=usage, prices=prices)
    assert cost is not None
    assert cost > 0


def test_no_book_or_no_model_leaves_the_turn_unpriced() -> None:
    usage = Usage(input_tokens=1000, output_tokens=500)
    assert turn_cost(model="deepseek-v4-flash", usage=usage, prices=None) is None
    assert turn_cost(model=None, usage=usage, prices=PriceBook(load_archive(), {})) is None


def test_a_survivor_fault_names_the_count_and_the_reason() -> None:
    fault = survivor_fault(3, reason="session closed")
    assert fault.error == "3 descendant(s) survived session closed; inspect before reuse"
    assert fault.turn_id is None


def subject(
    *,
    parts: tuple[str, ...] = ("hello", "there"),
    result: dict[str, object] | None = None,
    interrupted: bool = False,
    denials: tuple[str, ...] = (),
    started_at: float | None = 100.0,
) -> TurnSubject:
    return TurnSubject(
        turn_id="turn-1",
        session_id="session-1",
        parts=parts,
        result={"stopReason": "end_turn", "subtype": "success"} if result is None else result,
        interrupt_requested=interrupted,
        permission_denials=denials,
        started_at=started_at,
    )


def measurement(
    *,
    observed: UsageSnapshot | None = None,
    model: str | None = "deepseek-v4-flash",
    prices: PriceBook | None = None,
    totals: SessionTotals | None = None,
) -> TurnMeasurement:
    return TurnMeasurement(
        observed=observed,
        model=model,
        prices=prices,
        totals=SessionTotals() if totals is None else totals,
    )


def test_a_settled_turn_reports_its_text_and_the_parts_it_was_made_of() -> None:
    settled = settle_turn(subject(), measurement(), now_monotonic=100.25)
    assert isinstance(settled, SettledTurn)
    assert settled.outcome.text == "hello\nthere"
    assert settled.outcome.subtype == "success"
    assert settled.outcome.interrupted is False
    assert settled.completed.text == "hello\nthere"
    assert settled.completed.num_turns == 2
    assert settled.completed.duration_ms == 250
    assert settled.assistant is not None and settled.assistant.text == "hello\nthere"


def test_an_empty_turn_reports_no_assistant_text() -> None:
    settled = settle_turn(subject(parts=()), measurement(), now_monotonic=100.0)
    assert settled.outcome.text == ""
    assert settled.assistant is None
    assert settled.completed.duration_ms == 0


def test_an_interrupt_request_marks_the_turn_even_when_the_runtime_says_otherwise() -> None:
    settled = settle_turn(subject(interrupted=True), measurement(), now_monotonic=100.0)
    assert settled.outcome.interrupted is True


def test_the_runtime_s_own_interrupt_marks_the_turn_too() -> None:
    settled = settle_turn(
        subject(result={"stopReason": "cancelled", "subtype": "interrupted", "interrupted": True}),
        measurement(),
        now_monotonic=100.0,
    )
    assert settled.outcome.interrupted is True
    assert settled.outcome.stop_reason == "cancelled"


def test_the_permission_denials_travel_into_the_report() -> None:
    settled = settle_turn(subject(denials=("write file",)), measurement(), now_monotonic=100.0)
    assert settled.outcome.permission_denials == ("write file",)
    assert settled.completed.permission_denials == ("write file",)


def test_an_unmeasured_turn_adds_no_tokens_and_no_cost() -> None:
    settled = settle_turn(subject(), measurement(observed=None), now_monotonic=100.0)
    assert settled.outcome.usage == Usage()
    assert settled.outcome.per_turn_usage is False
    assert settled.outcome.cost_usd is None
    assert settled.totals.turns == 1
    assert settled.totals.cost_known is False
    assert settled.completed.cumulative_cost_usd is None


def test_a_measured_turn_carries_its_tokens_and_the_running_cost() -> None:
    observed = snapshot(StepUsage(1, 1, 100, 50, 0, 0))
    prices = PriceBook(load_archive(), {})
    settled = settle_turn(
        subject(), measurement(observed=observed, prices=prices), now_monotonic=100.0
    )
    assert settled.outcome.usage.input_tokens == 100
    assert settled.outcome.usage.output_tokens == 50
    assert settled.outcome.per_turn_usage is True
    assert settled.outcome.cost_usd is not None and settled.outcome.cost_usd > 0
    assert settled.completed.cumulative_cost_usd == settled.outcome.cost_usd
    assert settled.totals.cost_known is True


def test_the_totals_carry_the_previous_turns_forward() -> None:
    observed = snapshot(StepUsage(1, 1, 10, 5, 0, 0))
    settled = settle_turn(
        subject(),
        measurement(observed=observed, totals=SessionTotals(input_tokens=100, turns=2)),
        now_monotonic=100.0,
    )
    assert settled.totals.input_tokens == 110
    assert settled.totals.turns == 3


def test_a_turn_that_never_started_is_timed_from_the_settlement() -> None:
    settled = settle_turn(subject(started_at=None), measurement(), now_monotonic=100.0)
    assert settled.completed.duration_ms == 0

from __future__ import annotations

from base.lm.harness.codex.turns import (
    Bind,
    ErrorReport,
    Inbound,
    ItemStep,
    NotificationKind,
    TurnRecord,
    binding,
    error_report,
    inbound,
    item,
    settle,
    unclaimed_text,
)
from base.lm.harness.events import TurnCompleted, Usage
from base.lm.harness.turn import SessionTotals, TurnOutcome


def test_every_read_notification_is_classified_by_its_method() -> None:
    for method, kind in (
        ("item/agentMessage/delta", NotificationKind.TEXT_DELTA),
        ("item/reasoning/summaryTextDelta", NotificationKind.REASONING_DELTA),
        ("item/reasoning/textDelta", NotificationKind.REASONING_DELTA),
        ("item/started", NotificationKind.ITEM_STARTED),
        ("item/completed", NotificationKind.ITEM_COMPLETED),
        ("thread/tokenUsage/updated", NotificationKind.USAGE),
        ("turn/started", NotificationKind.TURN_STARTED),
        ("turn/completed", NotificationKind.TURN_COMPLETED),
        ("error", NotificationKind.ERROR),
    ):
        message = inbound(method, {}, session_id=None)
        assert message is not None
        assert message.kind is kind


def test_a_notification_names_the_turn_it_reports_on() -> None:
    message = inbound(
        "item/agentMessage/delta",
        {"threadId": "thread-1", "turnId": "codex-turn-2"},
        session_id="thread-1",
    )
    assert message == Inbound(NotificationKind.TEXT_DELTA, "codex-turn-2")


def test_a_completion_carries_the_turn_object_it_reports() -> None:
    turn = {"id": "codex-turn-2", "status": "completed"}
    message = inbound(
        "turn/completed", {"threadId": "thread-1", "turn": turn}, session_id="thread-1"
    )
    assert message == Inbound(NotificationKind.TURN_COMPLETED, "codex-turn-2", turn)


def test_a_completion_naming_no_turn_adopts_no_ticket() -> None:
    message = inbound("turn/completed", {"turn": {"status": "completed"}}, session_id=None)
    assert message is not None
    assert message.turn_id == ""


def test_a_notification_from_another_thread_is_not_ours() -> None:
    assert (
        inbound(
            "item/agentMessage/delta",
            {"threadId": "thread-elsewhere", "turnId": "codex-turn-2"},
            session_id="thread-1",
        )
        is None
    )
    assert (
        inbound("item/agentMessage/delta", {"threadId": "thread-elsewhere"}, session_id=None)
        is not None
    )


def test_a_method_the_bridge_does_not_model_is_dropped() -> None:
    assert inbound("item/plan/delta", {"turnId": "codex-turn-2"}, session_id="thread-1") is None


def test_a_completed_message_item_carries_the_final_answer() -> None:
    step = item(
        NotificationKind.ITEM_COMPLETED,
        {"item": {"type": "agentMessage", "text": "done", "phase": "final_answer"}},
    )
    assert step == ItemStep(phase="completed", text="done", final=True)


def test_a_commentary_item_is_reply_text_but_not_the_final_answer() -> None:
    step = item(
        NotificationKind.ITEM_COMPLETED,
        {"item": {"type": "agentMessage", "text": "looking at it", "phase": "commentary"}},
    )
    assert (step.text, step.final) == ("looking at it", False)


def test_a_started_item_reports_its_phase_and_no_reply() -> None:
    step = item(
        NotificationKind.ITEM_STARTED, {"item": {"type": "agentMessage", "text": "looking at it"}}
    )
    assert (step.phase, step.text, step.final) == ("started", None, False)


def test_an_item_that_is_not_a_reply_carries_no_text() -> None:
    step = item(NotificationKind.ITEM_COMPLETED, {"item": {"type": "commandExecution", "id": "e1"}})
    assert (step.phase, step.text, step.final) == ("completed", None, False)


def test_a_name_is_adopted_only_while_the_turn_is_starting() -> None:
    announced = {"turn": {"id": "codex-turn-3"}}
    assert binding(announced, previous=None, starting=True) == Bind("codex-turn-3", discard=False)
    assert binding(announced, previous=None, starting=False) is None


def test_a_name_that_changed_mid_turn_discards_the_old_partial_state() -> None:
    bound = binding({"turn": {"id": "codex-turn-3"}}, previous="codex-turn-2", starting=True)
    assert bound is not None
    assert bound.discard is True


def test_the_same_name_again_keeps_the_partial_state() -> None:
    assert binding(
        {"turn": {"id": "codex-turn-3"}}, previous="codex-turn-3", starting=True
    ) == Bind("codex-turn-3", discard=False)


def test_a_message_naming_no_turn_is_not_adopted() -> None:
    assert binding({"turn": {"status": "inProgress"}}, previous=None, starting=True) is None
    assert binding({}, previous=None, starting=True) is None


def test_a_retryable_error_says_nothing() -> None:
    assert error_report({"willRetry": True, "error": {"message": "rate limited"}}) is None


def test_an_error_names_its_text_and_the_turn_it_reports_on() -> None:
    report = error_report({"turnId": "codex-turn-4", "error": {"message": "usage limit"}})
    assert report == ErrorReport(text="usage limit", turn_id="codex-turn-4")


def test_an_error_without_a_message_still_reports_one() -> None:
    assert error_report({}) == ErrorReport(text="app-server reported an error", turn_id=None)


def test_the_reply_of_a_foreign_turn_is_every_message_it_carries() -> None:
    turn = {
        "items": [
            {"type": "agentMessage", "text": "first"},
            {"type": "commandExecution", "id": "e1"},
            {"type": "agentMessage", "text": "second"},
        ]
    }
    assert unclaimed_text(turn) == "first\nsecond"


def test_a_foreign_turn_without_items_has_no_reply() -> None:
    assert unclaimed_text({}) == ""


def test_a_usage_report_accumulates_and_counts_a_step() -> None:
    record = (
        TurnRecord()
        .with_usage(Usage(input_tokens=10, output_tokens=2))
        .with_usage(Usage(input_tokens=5, output_tokens=1))
    )
    assert record.usage == Usage(input_tokens=15, output_tokens=3)
    assert record.steps == (
        Usage(input_tokens=10, output_tokens=2),
        Usage(input_tokens=5, output_tokens=1),
    )


def test_a_report_of_no_tokens_is_not_a_step() -> None:
    assert TurnRecord().with_usage(Usage()) == TurnRecord()


def test_the_reply_prefers_the_final_answers() -> None:
    record = TurnRecord().with_text("looking at it", final=False).with_text("done", final=True)
    assert (record.parts, record.finals, record.text) == (
        ("looking at it", "done"),
        ("done",),
        "done",
    )


def test_a_turn_that_named_no_final_answer_keeps_everything_it_streamed() -> None:
    record = TurnRecord().with_text("one", final=False).with_text("two", final=False)
    assert record.text == "one\ntwo"


def test_declining_and_interrupting_are_recorded_on_the_turn() -> None:
    record = TurnRecord().with_denial("command: rm -rf build").interrupted()
    assert (record.denials, record.interrupt_requested) == (("command: rm -rf build",), True)


def test_a_completed_turn_settles_its_outcome_event_and_totals() -> None:
    usage = Usage(input_tokens=120, output_tokens=15, cache_read_input_tokens=80)
    record = (
        TurnRecord()
        .with_usage(usage)
        .with_text("All clear.", final=True)
        .with_denial("file change")
    )
    settlement = settle(
        turn_id="turn-1",
        session_id="thread-1",
        turn={"id": "codex-turn-1", "status": "completed", "durationMs": 12},
        record=record,
        totals=SessionTotals(input_tokens=5, turns=2),
    )
    assert settlement.outcome == TurnOutcome(
        turn_id="turn-1",
        text="All clear.",
        subtype="completed",
        is_error=False,
        interrupted=False,
        stop_reason=None,
        usage=usage,
        cost_usd=None,
        permission_denials=("file change",),
        num_turns=1,
        steps=(usage,),
    )
    assert settlement.completed == TurnCompleted(
        turn_id="turn-1",
        session_id="thread-1",
        text="All clear.",
        subtype="completed",
        is_error=False,
        interrupted=False,
        stop_reason=None,
        num_turns=1,
        duration_ms=12,
        usage=usage,
        cost_usd=None,
        cumulative_cost_usd=None,
        permission_denials=("file change",),
        steps=(usage,),
    )
    assert (settlement.totals.input_tokens, settlement.totals.output_tokens) == (125, 15)
    assert (settlement.totals.cache_read_input_tokens, settlement.totals.turns) == (80, 3)
    assert settlement.totals.cost_known is False


def test_a_failed_turn_settles_as_an_error_naming_the_servers_reason() -> None:
    settlement = settle(
        turn_id="turn-2",
        session_id=None,
        turn={"status": "failed", "error": {"message": "usage limit reached"}},
        record=TurnRecord(),
        totals=SessionTotals(),
    )
    assert (settlement.outcome.is_error, settlement.outcome.subtype) == (True, "failed")
    assert settlement.outcome.stop_reason == "usage limit reached"
    assert settlement.completed.session_id is None
    assert settlement.outcome.usage == Usage()


def test_an_interrupt_the_server_never_reports_still_settles_as_interrupted() -> None:
    settlement = settle(
        turn_id="turn-3",
        session_id="thread-1",
        turn={"status": "completed"},
        record=TurnRecord().interrupted(),
        totals=SessionTotals(),
    )
    assert (settlement.outcome.interrupted, settlement.outcome.is_error) == (True, False)


def test_a_step_per_report_names_the_number_of_steps() -> None:
    record = TurnRecord().with_usage(Usage(input_tokens=1)).with_usage(Usage(output_tokens=1))
    settlement = settle(
        turn_id="turn-4",
        session_id=None,
        turn={"status": "completed"},
        record=record,
        totals=SessionTotals(),
    )
    assert (settlement.outcome.num_turns, settlement.completed.num_turns) == (2, 2)
    assert len(settlement.outcome.steps) == 2

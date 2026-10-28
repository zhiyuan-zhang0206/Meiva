from __future__ import annotations

from base.lm.harness.claude.inbound import (
    BackgroundTasks,
    MessageKind,
    SessionIdentity,
    assistant_texts,
    claimed_origin,
    counts_as_step,
    message_kind,
    notification_plan,
    started_identity,
)
from base.lm.harness.events import (
    AssistantText,
    RateLimitObserved,
    ReasoningText,
    SessionStarted,
    TextDelta,
    ToolUse,
    UnsolicitedOutput,
)

REPORT = UnsolicitedOutput(source="task-notification", text="Background task t1 completed.")


def test_the_class_name_decides_which_kind_a_message_is() -> None:
    assert message_kind("ResultMessage") is MessageKind.RESULT
    assert message_kind("TaskNotificationMessage") is MessageKind.TASK_NOTIFICATION
    assert message_kind("TaskStartedMessage") is MessageKind.TASK_STARTED
    assert message_kind("TaskUpdatedMessage") is MessageKind.TASK_UPDATED
    assert message_kind("RateLimitEvent") is MessageKind.SESSION
    assert message_kind("SystemMessage") is MessageKind.SESSION
    assert message_kind("AssistantMessage") is MessageKind.TURN
    assert message_kind("AMessageAFutureSdkAdds") is MessageKind.TURN


def test_only_an_origin_that_names_no_turn_can_claim_the_running_one() -> None:
    assert claimed_origin("human") is None
    assert claimed_origin(None) is None
    assert claimed_origin("task-notification") == "task-notification"


def test_a_step_is_an_assistant_message_that_produced_text_or_a_tool_call() -> None:
    text = AssistantText(turn_id="turn-1", text="hi")
    tool = ToolUse(turn_id="turn-1", tool_use_id="toolu-1", name="Bash", tool_input={})
    reasoning = ReasoningText(turn_id="turn-1", text="thinking")

    assert counts_as_step("AssistantMessage", [text]) is True
    assert counts_as_step("AssistantMessage", [reasoning, tool]) is True
    assert counts_as_step("AssistantMessage", [reasoning]) is False
    assert counts_as_step("UserMessage", [text]) is False


def test_the_turns_text_is_its_assistant_text_and_nothing_else() -> None:
    events = [
        TextDelta(turn_id="turn-1", text="par"),
        AssistantText(turn_id="turn-1", text="one"),
        ReasoningText(turn_id="turn-1", text="a thought"),
        AssistantText(turn_id="turn-1", text="two"),
    ]

    assert assistant_texts(events) == ("one", "two")
    assert assistant_texts([]) == ()


def test_a_report_nobody_has_read_goes_to_the_sink_on_its_own() -> None:
    plan = notification_plan([REPORT], claimed=False)

    assert plan.report is REPORT
    assert plan.stream == ()


def test_a_report_inside_a_running_turn_stays_in_the_stream() -> None:
    plan = notification_plan([REPORT], claimed=True)

    assert plan.report is None
    assert plan.stream == (REPORT,)


def test_a_message_without_a_report_streams_whole_and_keeps_its_order() -> None:
    signal = RateLimitObserved(
        status="allowed", utilization=None, resets_at=None, rate_limit_type=None
    )

    assert notification_plan([signal], claimed=False).report is None
    assert notification_plan([signal], claimed=False).stream == (signal,)

    plan = notification_plan([signal, REPORT], claimed=False)
    assert plan.stream == (signal,)
    assert plan.report is REPORT


def test_a_started_task_stays_open_until_a_report_or_a_terminal_update() -> None:
    tasks = BackgroundTasks().started("t1").started("t2")

    assert tasks.open_task_ids == ("t1", "t2")
    assert tasks.reported("t1").open_task_ids == ("t2",)
    assert tasks.started("").open_task_ids == ("t1", "t2")
    assert BackgroundTasks().open_task_ids == ()


def test_only_a_terminal_status_closes_a_task() -> None:
    tasks = BackgroundTasks().started("t1").started("t2")

    for status in ("completed", "failed", "stopped", "killed"):
        assert tasks.updated("t1", status).open_task_ids == ("t2",), status
    assert tasks.updated("t1", "running").open_task_ids == ("t1", "t2")
    assert tasks.updated("t1", None).open_task_ids == ("t1", "t2")


def test_the_init_names_the_session_and_the_model_unless_it_reports_neither() -> None:
    named = SessionStarted(session_id="sess-a", model="model-a", cwd=None)
    blank = SessionStarted(session_id="", model=None, cwd=None)

    assert started_identity(named, session_id="sess-old", model="model-old") == SessionIdentity(
        session_id="sess-a", model="model-a"
    )
    assert started_identity(blank, session_id="sess-old", model="model-old") == SessionIdentity(
        session_id="sess-old", model="model-old"
    )

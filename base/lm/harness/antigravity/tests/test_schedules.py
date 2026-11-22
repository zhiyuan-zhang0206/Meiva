from __future__ import annotations

import math
from pathlib import Path

import pytest

from base.lm.harness.antigravity.config import AntigravityConfig
from base.lm.harness.antigravity.schedules import (
    UNBOUNDED_SCHEDULE_SECONDS,
    Schedules,
)
from base.lm.harness.session import BackgroundKind

NOW = 1_794_329_400.0


# meiva:note
# No recording of the `schedule` tool's parameters exists; the parameter name `DurationSeconds` is the
# one the CLI's changelog names, and everything else about the call is treated as opaque.
# meiva:link base/lm/harness/antigravity/docs/antigravity.md
def _scheduled(params: dict[str, object], *, is_error: bool = False) -> Schedules:
    work = Schedules().tool_used("s1", "schedule", params)
    return work.tool_returned("s1", is_error=is_error, now=NOW)


def test_a_successful_schedule_call_is_pending_for_a_day_when_it_names_no_duration() -> None:
    work = _scheduled({"Cron": "*/5 * * * *", "Prompt": "check CI"})
    [item] = work.pending_at(NOW)
    assert item.kind is BackgroundKind.SCHEDULE
    assert "check CI" in item.description
    assert work.pending_at(NOW + UNBOUNDED_SCHEDULE_SECONDS - 1)
    assert not work.pending_at(NOW + UNBOUNDED_SCHEDULE_SECONDS + 1)


def test_a_timer_with_a_duration_adds_nothing_after_its_blocking_call_returns() -> None:
    for value in (120, "120"):
        work = _scheduled({"DurationSeconds": value, "Prompt": "ping"})
        assert not work.pending_at(NOW)


def test_detached_timer_keeps_the_process_after_due_and_through_its_reply() -> None:
    work = Schedules(reply_wait_seconds=30).tool_used(
        "timer",
        "schedule",
        {"DurationSeconds": 5},
        turn_id="turn",
        monotonic_now=100,
    )
    work = work.finish_turn("turn", now=NOW, monotonic_now=100)
    assert work.reply_deadline(NOW + 4) is None
    assert work.reply_deadline(NOW + 5) == NOW + 35
    assert work.pending_at(NOW + 6)

    work = work.tool_returned("timer", is_error=False, now=NOW + 8)
    assert work.pending_at(NOW + 14)
    assert work.reply_deadline(NOW + 14) == NOW + 38
    work = work.reply_finished()
    assert not work.pending_at(NOW + 14)
    assert work.reply_deadline(NOW + 14) is None


def test_a_failed_or_other_tool_call_schedules_nothing() -> None:
    assert not _scheduled({"Prompt": "x"}, is_error=True).pending_at(NOW)
    other = Schedules().tool_used("t", "run_command", {"x": 1})
    assert other == Schedules()


def test_timer_duration_parsing_caps_bounds_and_rejects_non_finite_values(tmp_path: Path) -> None:
    mono = 1000.0
    work = Schedules(max_timer_seconds=60.0)
    work = work.tool_used(
        "s1",
        "schedule",
        {"DurationSeconds": "1.5", "Prompt": "short"},
        turn_id="t1",
        monotonic_now=mono,
    )
    assert work.silence_floor_monotonic(("t1",)) == 1001.5

    capped = Schedules(max_timer_seconds=60.0).tool_used(
        "s2",
        "schedule",
        {"DurationSeconds": 999999, "Prompt": "huge"},
        turn_id="t1",
        monotonic_now=mono,
    )
    assert capped.silence_floor_monotonic(("t1",)) == 1060.0

    for bad in (
        0,
        -5,
        "-1.5",
        True,
        False,
        float("nan"),
        float("inf"),
        float("-inf"),
        "nan",
        "inf",
        "abc",
    ):
        invalid = Schedules().tool_used(
            "s-bad",
            "schedule",
            {"DurationSeconds": bad, "Prompt": "bad"},
            turn_id="t1",
            monotonic_now=mono,
        )
        assert invalid.silence_floor_monotonic(("t1",)) is None

    for bad_bound in (0.0, -10.0, math.nan, math.inf):
        with pytest.raises(ValueError, match="max_schedule_timer_seconds"):
            _ = AntigravityConfig(cwd=tmp_path, max_schedule_timer_seconds=bad_bound)


def test_async_timer_survives_unconditional_activity_and_clears_on_manage_task_kill() -> None:
    mono = 500.0
    work = Schedules()
    work = work.tool_used(
        "s1",
        "schedule",
        {"DurationSeconds": 1800, "Prompt": "check metrics"},
        turn_id="t1",
        monotonic_now=mono,
    )
    work = work.tool_returned(
        "s1",
        is_error=False,
        now=NOW,
        content="Tool is running as a background task with task id: conv-1/task-384",
        monotonic_now=mono + 1.0,
    )
    assert work.silence_floor_monotonic(("t1",)) == mono + 1800.0
    work = work.on_assistant_text("t1", monotonic_now=mono + 10.0)
    work = work.on_input("t1", monotonic_now=mono + 60.0)
    work = work.on_assistant_text("t1", monotonic_now=mono + 65.0)
    assert work.silence_floor_monotonic(("t1",)) == mono + 1800.0

    work = work.tool_used(
        "k-other",
        "manage_task",
        {"Action": "kill", "TaskId": "conv-1/task-999"},
        turn_id="t1",
        monotonic_now=mono + 70.0,
    )
    work = work.tool_returned("k-other", is_error=False, now=NOW, monotonic_now=mono + 71.0)
    assert work.silence_floor_monotonic(("t1",)) == mono + 1800.0

    work = work.tool_used(
        "k-fail",
        "manage_task",
        {"Action": "kill", "TaskId": "conv-1/task-384"},
        turn_id="t1",
        monotonic_now=mono + 72.0,
    )
    work = work.tool_returned("k-fail", is_error=True, now=NOW, monotonic_now=mono + 73.0)
    assert work.silence_floor_monotonic(("t1",)) == mono + 1800.0

    work = work.tool_used(
        "k-ok",
        "manage_task",
        {"Action": "kill", "TaskId": "conv-1/task-384"},
        turn_id="t1",
        monotonic_now=mono + 74.0,
    )
    work = work.tool_returned("k-ok", is_error=False, now=NOW, monotonic_now=mono + 75.0)
    assert work.silence_floor_monotonic(("t1",)) is None


def test_conditional_timer_evicts_on_early_wakeup_and_floors_are_isolated_per_turn() -> None:
    mono = 100.0
    work = Schedules()
    work = work.tool_used(
        "s1",
        "schedule",
        {"DurationSeconds": 1800, "TimerCondition": "any", "Prompt": "wait"},
        turn_id="t1",
        monotonic_now=mono,
    )
    work = work.tool_returned(
        "s1",
        is_error=False,
        now=NOW,
        content="Tool is running as a background task with task id: conv-1/task-10",
        monotonic_now=mono + 1.0,
    )
    assert work.silence_floor_monotonic(("t1",)) == mono + 1800.0
    assert work.silence_floor_monotonic(("t1", "t2")) is None

    after_input = work.on_input("t1", monotonic_now=mono + 30.0)
    assert after_input.silence_floor_monotonic(("t1",)) is None

    after_tool = work.tool_used(
        "r1", "read_file", {"Path": "/x"}, turn_id="t1", monotonic_now=mono + 30.0
    )
    assert after_tool.silence_floor_monotonic(("t1",)) is None

    uncond = Schedules().tool_used(
        "u1",
        "schedule",
        {"DurationSeconds": 60, "Prompt": "tick"},
        turn_id="t1",
        monotonic_now=mono,
    )
    uncond = uncond.tool_returned(
        "u1", is_error=False, now=NOW, content="task id: conv-1/task-11", monotonic_now=mono + 1.0
    )
    assert uncond.silence_floor_monotonic(("t1",)) == mono + 60.0
    assert (
        uncond.on_assistant_text("t1", monotonic_now=mono + 65.0).silence_floor_monotonic(("t1",))
        is None
    )
    assert (
        uncond.finish_turn("t1", now=0, monotonic_now=mono).silence_floor_monotonic(("t1",)) is None
    )

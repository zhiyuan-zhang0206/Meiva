from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from base.clock import SYSTEM_TIMERS, Clock, ClockConfig
from base.clock.tests.manual_timers import ManualTimers
from base.lm.harness.antigravity.tests.support import Harness, release_timer, start
from base.lm.harness.delivery import HarnessInput, Reconciliation
from base.lm.harness.events import (
    AssistantText,
    HarnessCause,
    InputInContext,
    SessionFailed,
    ToolResult,
    ToolUse,
    TurnCompleted,
    TurnFaulted,
    TurnOrigin,
    TurnStarted,
    UnsolicitedOutput,
    Usage,
)
from base.lm.harness.registry import FileSessionRegistry


async def _next_prompt(harness: Harness, command: str = "next") -> TurnCompleted:
    await harness.session.deliver([HarnessInput.of("next", command)])
    done = await harness.wait_for(lambda e: isinstance(e, TurnCompleted) and e.text == "OK")
    assert isinstance(done, TurnCompleted)
    assert done.usage == Usage(40, 5, 0, 30)
    assert not harness.session.background_work().items
    assert not any(isinstance(e, SessionFailed | TurnFaulted) for e in harness.events)
    return done


async def _wait_for_keepalive_removal(harness: Harness, registry: FileSessionRegistry) -> None:
    async with asyncio.timeout(3):
        while True:
            assert [e.error for e in harness.events if isinstance(e, SessionFailed)] == []
            record = registry.load(1, harness="antigravity")
            if record is not None and not record.background:
                return
            await asyncio.sleep(0.005)


async def test_unverified_background_tail_cannot_hold_foreground_completion(tmp_path: Path) -> None:
    harness = await start(
        tmp_path, extra_args=("--test-missing-background-planner",), reply_seconds=2
    )
    try:
        await harness.session.deliver([HarnessInput.of("timer", "background")])
        timer = await harness.wait_for(lambda e: isinstance(e, TurnCompleted))
        await harness.session.deliver([HarnessInput.of("first", "overlap-first")])
        await harness.wait_for(lambda e: isinstance(e, InputInContext) and e.key == "first")
        await release_timer(harness)
        harness.release("release-foreground")
        await harness.wait_for(lambda e: isinstance(e, AssistantText) and e.text == "FOREGROUND")
        harness.release()
        await harness.wait_for_marker("background-sent")
        harness.release("release-front-result")
        done = await harness.wait_for(
            lambda e: (
                isinstance(e, TurnCompleted)
                and e.turn_id != timer.turn_id
                and e.text != "BACKGROUND"
            )
        )
        assert done.text == "FOREGROUND"
        assert harness.session.background_work().items
        pid = harness.session.cli_pid
        await _next_prompt(harness)
        assert harness.session.cli_pid == pid
        assert (
            sum(isinstance(e, TurnCompleted) and e.turn_id == done.turn_id for e in harness.events)
            == 1
        )
        assert not any(isinstance(e, UnsolicitedOutput) for e in harness.events)
    finally:
        await harness.stop()


@pytest.mark.parametrize("completed_timer", [False, True])
async def test_expired_detached_timer_leaves_steered_foreground_alive(
    tmp_path: Path, completed_timer: bool
) -> None:
    wall = ManualTimers()
    clock = Clock(ClockConfig("UTC", "UTC", False), timers=SYSTEM_TIMERS, now=wall.now)
    registry = FileSessionRegistry(tmp_path / "registry")
    harness = await start(
        tmp_path,
        extra_args=("--test-overlap", "--test-four-minute-timer", "--test-early-command-result"),
        clock=clock,
        registry=registry,
    )
    try:
        await harness.session.deliver([HarnessInput.of("timer", "background")])
        await harness.wait_for_type(TurnCompleted)
        assert harness.session.background_work().items
        pid = harness.session.cli_pid
        wall.advance(198)
        await harness.session.deliver([HarnessInput.of("first", "hold")])
        tool = await harness.wait_for(
            lambda e: isinstance(e, ToolUse) and e.tool_use_id == "tool-1"
        )
        wall.advance(13)
        await harness.session.deliver([HarnessInput.of("steer", "steer")])
        await harness.wait_for(lambda e: isinstance(e, InputInContext) and e.key == "steer")
        wall.advance(6)
        await harness.wait_for(
            lambda e: e == ToolResult(tool.turn_id, "tool-1", False, "WORK_DONE")
        )
        if completed_timer:
            await release_timer(harness)

        wall.advance(31 if completed_timer else 54)
        await _wait_for_keepalive_removal(harness, registry)
        assert not harness.session.background_work().items
        assert harness.session.active_turn_id == tool.turn_id
        harness.release("release-steer")
        done = await harness.wait_for(
            lambda e: isinstance(e, TurnCompleted) and e.turn_id == tool.turn_id
        )
        assert done.text == "ORIGINAL_DONE\nSTEER_DONE"
        assert harness.session.cli_pid == pid
    finally:
        await harness.stop()


@pytest.mark.parametrize("late_reply", [False, True])
async def test_idle_timer_without_a_reply_releases_waiting_input_on_the_same_peer(
    tmp_path: Path, late_reply: bool
) -> None:
    args = ("--test-early-hub",) if late_reply else ()
    harness = await start(tmp_path, extra_args=args, reply_seconds=0.25)
    try:
        await harness.session.deliver([HarnessInput.of("timer", "background")])
        await harness.wait_for_type(TurnCompleted)
        pid = harness.session.cli_pid
        await release_timer(harness)
        command = "late-timer" if late_reply else "next"
        done = await _next_prompt(harness, command)
        if late_reply:
            native = await harness.wait_for(
                lambda e: isinstance(e, TurnCompleted) and e.text == "BACKGROUND"
            )
            assert native.turn_id != done.turn_id and native.usage == Usage(267, 3, 0, 30)
        assert await harness.session.reconcile(["next"]) == {"next": Reconciliation.IN_CONTEXT}
        assert harness.session.cli_pid == pid
        assert len(harness.prompts()) == 2
    finally:
        await harness.stop()


async def test_live_timer_notice_survives_steer_and_keeps_separate_usage(tmp_path: Path) -> None:
    harness = await start(
        tmp_path, extra_args=("--test-overlap", "--test-early-hub", "--test-timer-before-steer")
    )
    try:
        await harness.session.deliver([HarnessInput.of("timer", "background")])
        await harness.wait_for_type(TurnCompleted)
        await harness.session.deliver([HarnessInput.of("first", "hold")])
        tool = await harness.wait_for(
            lambda e: isinstance(e, ToolUse) and e.tool_use_id == "tool-1"
        )
        await release_timer(harness)
        await harness.session.deliver([HarnessInput.of("steer", "steer")])
        harness.release("release-steer")
        done = await harness.wait_for(
            lambda e: isinstance(e, TurnCompleted) and e.turn_id == tool.turn_id
        )
        begun = await harness.wait_for(
            lambda e: isinstance(e, TurnStarted) and e.origin is TurnOrigin.HARNESS
        )
        native = await harness.wait_for(
            lambda e: isinstance(e, TurnCompleted) and e.turn_id == begun.turn_id
        )
        assert done.text == "STEER_DONE" and done.usage == Usage(80, 10, 0, 60)
        assert begun.cause is HarnessCause.SCHEDULED_WAKE and begun.turn_id != done.turn_id
        assert native.text == "BACKGROUND" and native.usage == Usage(267, 3, 0, 30)
        assert [
            e
            for e in harness.events
            if isinstance(e, TurnStarted) and e.origin is TurnOrigin.HARNESS
        ] == [begun]
        assert not harness.session.background_work().items
    finally:
        await harness.stop()


@pytest.mark.parametrize("stop_reason", ["end_turn", "max_tokens"])
@pytest.mark.parametrize("case", ["timer-no-usage", "child-fence"])
async def test_timer_expiry_preserves_fresh_foreground(
    tmp_path: Path, stop_reason: str, case: str
) -> None:
    args = ("--test-early-hub",) if case == "timer-no-usage" else ()
    harness = await start(tmp_path, extra_args=args, reply_seconds=0.25)
    try:
        await harness.session.deliver([HarnessInput.of("timer", "background")])
        await harness.wait_for_type(TurnCompleted)
        pid = harness.session.cli_pid
        await release_timer(harness)
        if case == "child-fence":
            await harness.session.deliver([HarnessInput.of("warmup", "child-seed")])
            await harness.wait_for(lambda e: isinstance(e, TurnCompleted) and e.text == "OK")
        await harness.session.deliver([HarnessInput.of("fresh", f"{case}:{stop_reason}")])
        begun = await harness.wait_for(
            lambda e: isinstance(e, TurnStarted) and e.keys == ("fresh",)
        )
        done = await harness.wait_for(
            lambda e: (
                (isinstance(e, TurnCompleted) and e.turn_id == begun.turn_id)
                or isinstance(e, SessionFailed)
            )
        )
        assert isinstance(done, TurnCompleted), done
        assert done.text == "FRESH"
        assert not done.usage_uncertain
        expected = Usage(240, 3, 0, 30) if case == "timer-no-usage" else Usage(40, 5, 0, 30)
        assert done.usage == expected
        assert not any(
            isinstance(e, TurnStarted) and e.origin is TurnOrigin.HARNESS for e in harness.events
        )
        assert harness.session.cli_pid == pid
    finally:
        await harness.stop()

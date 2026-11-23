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
    SessionClosed,
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
from base.lm.harness.process import PsutilChildProbe
from base.lm.harness.registry import FileSessionRegistry


async def _next_prompt(harness: Harness, command: str = "next") -> TurnCompleted:
    await harness.session.deliver([HarnessInput.of("next", command)])
    done = await harness.wait_for(lambda e: isinstance(e, TurnCompleted) and e.text == "OK")
    assert isinstance(done, TurnCompleted)
    assert done.usage == Usage(40, 5, 0, 30)
    assert not harness.session.background_work().items
    assert not any(isinstance(e, SessionFailed) for e in harness.events)
    return done


async def _wait_for_retired_peer(harness: Harness, pid: int) -> None:
    probe = PsutilChildProbe()
    async with asyncio.timeout(5):
        while probe.is_alive(pid):
            await asyncio.sleep(0.005)
    assert harness.session.cli_pid is None
    assert not any(
        isinstance(e, SessionClosed | SessionFailed | TurnFaulted) for e in harness.events
    )


def _assert_reopened(
    harness: Harness, old_pid: int, done: TurnCompleted, *, fault: TurnFaulted | None = None
) -> None:
    assert harness.session.cli_pid is not None and harness.session.cli_pid != old_pid
    assert not PsutilChildProbe().is_alive(old_pid)
    assert done.generation == 2 and done.session_id == "conv-acp"
    [load] = [frame for frame in harness.frames() if frame.get("method") == "session/load"]
    assert load["params"]["sessionId"] == done.session_id
    assert not any(isinstance(e, SessionClosed | SessionFailed) for e in harness.events)
    assert [e for e in harness.events if isinstance(e, TurnFaulted)] == (
        [] if fault is None else [fault]
    )


async def _wait_for_keepalive_removal(harness: Harness, registry: FileSessionRegistry) -> None:
    async with asyncio.timeout(3):
        while True:
            assert [e.error for e in harness.events if isinstance(e, SessionFailed)] == []
            record = registry.load(1, harness="antigravity")
            if record is not None and not record.background:
                return
            await asyncio.sleep(0.005)


@pytest.mark.parametrize("expires", [False, True])
async def test_native_echo_fence_preserves_another_live_timer(
    tmp_path: Path, expires: bool
) -> None:
    wall = ManualTimers()
    clock = Clock(ClockConfig("UTC", "UTC", False), timers=SYSTEM_TIMERS, now=wall.now)
    harness = await start(tmp_path, clock=clock, reply_seconds=0.2)
    try:
        await harness.session.deliver([HarnessInput.of("timer", "background")])
        await harness.wait_for_type(TurnCompleted)
        assert harness.session.background_work().items
        pid = harness.session.cli_pid
        await harness.session.deliver([HarnessInput.of("first", "unresolved-attribution")])
        await harness.wait_for(
            lambda e: isinstance(e, TurnCompleted) and e.text == "" and e.usage_uncertain
        )
        harness.release("delay-native-frame")
        harness.release("release-native")
        await harness.wait_for(
            lambda e: isinstance(e, TurnCompleted) and e.text == "LATE NATIVE REPLY"
        )
        started = asyncio.Event()

        async def send() -> TurnCompleted:
            started.set()
            return await _next_prompt(harness, "same-text-without-planner")

        async with asyncio.TaskGroup() as tasks:
            delivery = tasks.create_task(send())
            await started.wait()
            assert not delivery.done() and len(harness.prompts()) == 2
            assert harness.session.cli_pid == pid and harness.session.background_work().items
            if expires:
                wall.advance(6)
            else:
                await release_timer(harness)
                harness.release()
                native = await harness.wait_for(
                    lambda e: isinstance(e, TurnCompleted) and e.text == "BACKGROUND"
                )
                assert native.generation == 1
        assert pid is not None
        _assert_reopened(harness, pid, delivery.result())
        assert harness.session.take_lost_background() == ()
    finally:
        await harness.stop()


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
        assert pid is not None
        reopened = await _next_prompt(harness)
        _assert_reopened(harness, pid, reopened)
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
        assert pid is not None
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
        assert harness.session.cli_pid == pid
        harness.release("release-steer")
        done = await harness.wait_for(
            lambda e: isinstance(e, TurnCompleted) and e.turn_id == tool.turn_id
        )
        assert done.text == "ORIGINAL_DONE\nSTEER_DONE" and done.generation == 1
        await _wait_for_retired_peer(harness, pid)
        _assert_reopened(harness, pid, await _next_prompt(harness))
    finally:
        await harness.stop()


@pytest.mark.parametrize("early_hub", [False, True])
async def test_idle_expiry_recycles_before_dispatch_and_fences_the_old_timer(
    tmp_path: Path, early_hub: bool
) -> None:
    args = ("--test-overlap", "--test-early-hub") if early_hub else ("--test-overlap",)
    harness = await start(tmp_path, extra_args=args, reply_seconds=0.25)
    try:
        await harness.session.deliver([HarnessInput.of("timer", "background")])
        await harness.wait_for_type(TurnCompleted)
        pid = harness.session.cli_pid
        assert pid is not None
        await release_timer(harness)
        await harness.session.deliver([HarnessInput.of("first", "hold")])
        tool = await harness.wait_for(
            lambda e: isinstance(e, ToolUse) and e.tool_use_id == "tool-1"
        )
        assert not PsutilChildProbe().is_alive(pid)
        harness.release()
        await harness.session.deliver([HarnessInput.of("steer", "steer")])
        harness.release("release-steer")
        done = await harness.wait_for(
            lambda e: isinstance(e, TurnCompleted) and e.turn_id == tool.turn_id
        )
        assert done.text == "ORIGINAL_DONE\nSTEER_DONE"
        _assert_reopened(harness, pid, done)
        assert not (tmp_path / "background-sent").exists()
        assert await harness.session.reconcile(["first", "steer"]) == {
            "first": Reconciliation.IN_CONTEXT,
            "steer": Reconciliation.IN_CONTEXT,
        }
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
@pytest.mark.parametrize(
    ("case", "expected_text"),
    [("fresh-one", "ACTUAL_FRESH"), ("fresh-tool", "ACTUAL_FRESH"), ("fresh-textless", "")],
)
async def test_resumed_prompt_without_usage_ignores_timer_replay(
    tmp_path: Path, stop_reason: str, case: str, expected_text: str
) -> None:
    args = ("--test-early-hub", "--test-timer-replay-on-load")
    harness = await start(tmp_path, extra_args=args, reply_seconds=0.25)
    try:
        await harness.session.deliver([HarnessInput.of("timer", "background")])
        timer = await harness.wait_for_type(TurnCompleted)
        pid = harness.session.cli_pid
        assert pid is not None
        await release_timer(harness)
        await _wait_for_retired_peer(harness, pid)
        await harness.session.deliver([HarnessInput.of("fresh", f"{case}:{stop_reason}")])
        done = await harness.wait_for(
            lambda e: isinstance(e, TurnCompleted) and e.turn_id != timer.turn_id
        )
        _assert_reopened(harness, pid, done)
        assert done.text == expected_text and done.stop_reason == stop_reason
        assert not any(
            isinstance(e, ToolUse) and e.tool_use_id == "old-timer-command" for e in harness.events
        )
        assert not any(
            isinstance(e, TurnStarted) and e.origin is TurnOrigin.HARNESS for e in harness.events
        )
    finally:
        await harness.stop()


async def test_pending_timer_allows_two_steered_terminals_with_separate_receipts(
    tmp_path: Path,
) -> None:
    harness = await start(
        tmp_path,
        extra_args=(
            "--test-overlap",
            "--test-early-hub",
            "--test-timer-before-steer",
            "--test-terminal-before-steer",
        ),
    )
    try:
        await harness.session.deliver([HarnessInput.of("timer", "background")])
        await harness.wait_for_type(TurnCompleted)
        await harness.session.deliver([HarnessInput.of("first", "hold")])
        first = await harness.wait_for(
            lambda e: isinstance(e, AssistantText) and e.text == "FIRST_DONE"
        )
        await release_timer(harness)
        await harness.session.deliver([HarnessInput.of("steer", "steer")])
        harness.release("release-steer")
        done = await harness.wait_for(
            lambda e: isinstance(e, TurnCompleted) and e.turn_id == first.turn_id
        )
        assert done.text == "FIRST_DONE\nSTEER_DONE"
        assert done.usage == Usage(95, 11, 0, 60) and not done.usage_uncertain
        assert not any(
            isinstance(e, TurnStarted) and e.origin is TurnOrigin.HARNESS for e in harness.events
        )
        assert harness.session.background_work().items
    finally:
        await harness.stop()


async def test_prompt_rpc_fault_recycles_the_peer_after_timer_expiry(tmp_path: Path) -> None:
    wall = ManualTimers()
    clock = Clock(ClockConfig("UTC", "UTC", False), timers=SYSTEM_TIMERS, now=wall.now)
    registry = FileSessionRegistry(tmp_path / "registry")
    harness = await start(
        tmp_path, clock=clock, registry=registry, extra_args=("--test-fault-held-prompt",)
    )
    try:
        await harness.session.deliver([HarnessInput.of("timer", "background")])
        await harness.wait_for_type(TurnCompleted)
        old_pid = harness.session.cli_pid
        assert old_pid is not None
        await harness.session.deliver([HarnessInput.of("first", "hold")])
        tool = await harness.wait_for(
            lambda e: isinstance(e, ToolUse) and e.tool_use_id == "tool-1"
        )
        wall.advance(36)
        await _wait_for_keepalive_removal(harness, registry)
        assert harness.session.cli_pid == old_pid
        harness.release("release-fault")
        fault = await harness.wait_for_type(TurnFaulted)
        assert fault.turn_id == tool.turn_id and "provider fault" in fault.error
        _assert_reopened(harness, old_pid, await _next_prompt(harness), fault=fault)
    finally:
        await harness.stop()

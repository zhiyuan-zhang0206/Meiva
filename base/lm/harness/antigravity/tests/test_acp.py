from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path

import pytest

from base.lm.harness.antigravity.acp import input_marker
from base.lm.harness.antigravity.foreground import (
    PromptTurn,
    PromptUsage,
    bill_prompt,
    planner_usage,
    turn_usage,
)
from base.lm.harness.antigravity.mapping import OutputProjection
from base.lm.harness.antigravity.session import ActiveTurn
from base.lm.harness.antigravity.tests.support import Harness, start
from base.lm.harness.antigravity.timing_store import timing_path
from base.lm.harness.delivery import HarnessInput, ImageBlock, Reconciliation, TextBlock
from base.lm.harness.events import (
    AssistantText,
    InputInContext,
    InputLost,
    InputReceived,
    InputRejected,
    InterruptConfirmed,
    SessionFailed,
    ToolResult,
    ToolUse,
    TurnCompleted,
    TurnFaulted,
    UnsolicitedOutput,
    Usage,
)
from base.lm.harness.process import PsutilChildProbe
from base.lm.harness.session import InputsNotWrittenError, SessionClosedError


async def test_one_prompt_preserves_ordered_text_and_images_and_recovers_its_receipt(
    tmp_path: Path,
) -> None:
    harness = await start(tmp_path)
    image = ImageBlock("image/png", "YQ==")
    try:
        await harness.session.deliver(
            [HarnessInput("one", (TextBlock("look"), image, TextBlock("tail")))]
        )
        done = await harness.wait_for(lambda e: isinstance(e, TurnCompleted))
        [prompt] = harness.prompts()
        assert prompt["params"]["prompt"] == [
            {"type": "text", "text": f"{input_marker('one')}\nlook"},
            {"type": "image", "mimeType": "image/png", "data": "YQ=="},
            {"type": "text", "text": "tail"},
        ]
        assert done.text == "OK" and done.usage == Usage(40, 5, 0, 30)
        assert await harness.session.reconcile(["one", "absent"]) == {
            "one": Reconciliation.IN_CONTEXT,
            "absent": Reconciliation.UNKNOWN,
        }
    finally:
        await harness.stop()


async def test_superseded_result_leaves_same_turn_and_tool_running(tmp_path: Path) -> None:
    harness = await start(tmp_path)
    try:
        await harness.session.deliver([HarnessInput.of("original", "hold")])
        tool = await harness.wait_for(lambda e: isinstance(e, ToolUse))
        await harness.session.deliver(
            [HarnessInput.of("steer", "steer", [ImageBlock("image/png", "YQ==")])]
        )
        ack = await harness.wait_for(lambda e: isinstance(e, InputInContext) and e.key == "steer")
        [first, second] = harness.prompts()
        assert first["id"] != second["id"]
        assert second["params"]["prompt"][1]["type"] == "image"
        assert harness.session.active_turn_id == tool.turn_id == ack.turn_id
        assert not any(isinstance(e, TurnCompleted) for e in harness.events)
        harness.release()
        done = await harness.wait_for(lambda e: isinstance(e, TurnCompleted))
        assert done.turn_id == tool.turn_id
        assert done.text == "ORIGINAL_DONE\nSTEER_DONE"
        assert done.usage == Usage(80, 10, 0, 60)
        assert ToolResult(tool.turn_id, tool.tool_use_id, False, "WORK_DONE") in harness.events
        assert harness.session.active_turn_id is None
    finally:
        await harness.stop()


def _is_background(event: object) -> bool:
    return isinstance(event, TurnCompleted) and event.text == "BACKGROUND"


async def _release_timer(harness: Harness) -> ToolResult:
    harness.release("release-tool")
    return await harness.wait_for(lambda e: isinstance(e, ToolResult) and e.tool_use_id == "timer")


@pytest.mark.parametrize("extra_args", [(), ("--test-early-hub",)])
async def test_cancel_keeps_process_and_next_turn_works(
    tmp_path: Path, extra_args: tuple[str, ...]
) -> None:
    harness = await start(tmp_path, extra_args=extra_args)
    try:
        await harness.session.deliver([HarnessInput.of("timer", "background")])
        timer_turn = await harness.wait_for(lambda e: isinstance(e, TurnCompleted))
        pid = harness.session.cli_pid
        await harness.session.deliver([HarnessInput.of("hold", "hold")])
        tool = await harness.wait_for(
            lambda e: isinstance(e, ToolUse) and e.tool_use_id == "tool-1"
        )
        if extra_args:
            await _release_timer(harness)
        report = await harness.session.interrupt(turn_id=tool.turn_id)
        assert report.cli_alive and report.cli_pid == pid and report.settled
        cancel = [frame for frame in harness.frames() if frame.get("method") == "session/cancel"]
        assert cancel == [
            {"jsonrpc": "2.0", "method": "session/cancel", "params": {"sessionId": "conv-acp"}}
        ]
        await harness.wait_for(lambda e: isinstance(e, InterruptConfirmed))
        assert harness.session.background_work().items
        timer = await _release_timer(harness)
        assert timer.turn_id == timer_turn.turn_id and not timer.is_error
        harness.release()
        background = await harness.wait_for(_is_background)
        assert background.text == "BACKGROUND"
        await harness.session.deliver([HarnessInput.of("next", "after-cancel")])
        done = await harness.wait_for(lambda e: isinstance(e, TurnCompleted) and e.text == "OK")
        assert done.text == "OK" and harness.session.cli_pid == pid
        assert [
            event
            for event in harness.events
            if isinstance(event, ToolResult) and event.tool_use_id != "timer"
        ] == [
            ToolResult(tool.turn_id, "tool-1", is_error=True, content="Cancelled"),
            ToolResult(done.turn_id, "tool-2", is_error=False, content=""),
        ]
    finally:
        await harness.stop()


async def test_background_completion_and_answer_arrive_while_idle(tmp_path: Path) -> None:
    sink: list[UnsolicitedOutput] = []
    harness = await start(tmp_path, unsolicited=sink.append)
    try:
        await harness.session.deliver([HarnessInput.of("timer", "background")])
        done = await harness.wait_for(lambda e: isinstance(e, TurnCompleted))
        assert done.text == "TIMER_SET"
        assert harness.session.background_work().items
        harness.release("release-tool")
        await harness.wait_for(lambda e: isinstance(e, ToolResult) and e.tool_use_id == "timer")
        assert harness.session.background_work().items
        joined = asyncio.Event()

        async def send() -> None:
            joined.set()
            await harness.session.deliver([HarnessInput.of("next", "next")])

        async with asyncio.TaskGroup() as tasks:
            delivery = tasks.create_task(send())
            await joined.wait()
            assert not delivery.done()
            assert len(harness.prompts()) == 1
            harness.release()
        background = await harness.wait_for(_is_background)
        new = await harness.wait_for(
            lambda e: isinstance(e, TurnCompleted) and e.text == "OK" and e.turn_id != done.turn_id
        )
        assert background.text == "BACKGROUND" and sink == []
        assert new.text == "OK" and new.usage == Usage(40, 5, 0, 30)
        assert not harness.session.background_work().items
        assert len(harness.prompts()) == 2
    finally:
        await harness.stop()


async def test_detached_timer_reply_stays_out_of_an_already_active_turn(tmp_path: Path) -> None:
    harness = await start(tmp_path, extra_args=("--test-overlap",))
    try:
        await harness.session.deliver([HarnessInput.of("timer", "background")])
        await harness.wait_for(lambda e: isinstance(e, TurnCompleted))
        await harness.session.deliver([HarnessInput.of("first", "hold")])
        tool = await harness.wait_for(
            lambda e: isinstance(e, ToolUse) and e.tool_use_id == "tool-1"
        )
        await harness.session.deliver([HarnessInput.of("steer", "steer")])
        await harness.wait_for(lambda e: isinstance(e, InputInContext) and e.key == "steer")
        harness.release("release-tool")
        await harness.wait_for(lambda e: isinstance(e, ToolResult) and e.tool_use_id == "timer")
        harness.release()
        assert harness.session.active_turn_id == tool.turn_id
        harness.release("release-steer")
        done = await harness.wait_for(
            lambda e: isinstance(e, TurnCompleted) and e.turn_id == tool.turn_id
        )
        background = await harness.wait_for(_is_background)
        assert background.text == "BACKGROUND"
        assert done.text == "ORIGINAL_DONE\nSTEER_DONE"
        assert done.usage == Usage(80, 10, 0, 60) and not done.usage_uncertain
    finally:
        await harness.stop()


@pytest.mark.parametrize(
    "args",
    [
        (),
        ("--test-early-hub",),
        ("--test-background-row-first",),
        ("--test-early-hub", "--test-interleaved-chunks"),
        ("--test-early-hub", "--test-truncated-foreground"),
    ],
)
async def test_foreground_tool_and_usage_precede_the_detached_reply(
    tmp_path: Path, args: tuple[str, ...]
) -> None:
    harness = await start(tmp_path, extra_args=args)
    try:
        await harness.session.deliver([HarnessInput.of("timer", "background")])
        await harness.wait_for(lambda e: isinstance(e, TurnCompleted))
        await harness.session.deliver([HarnessInput.of("first", "overlap-first")])
        await harness.wait_for(lambda e: isinstance(e, InputInContext) and e.key == "first")
        assert len(harness.prompts()) == 2
        harness.release("release-tool")
        await harness.wait_for(lambda e: isinstance(e, ToolResult) and e.tool_use_id == "timer")
        harness.release("release-foreground")
        await harness.wait_for_marker("foreground-sent")
        if not args:
            assistant = await harness.wait_for(
                lambda e: isinstance(e, AssistantText) and e.text == "FOREGROUND"
            )
            assert assistant.turn_id == harness.session.active_turn_id
        assert not any(isinstance(e, UnsolicitedOutput) for e in harness.events)
        assert harness.session.background_work().items
        harness.release()
        harness.release("release-front-result")
        done = await harness.wait_for(
            lambda e: isinstance(e, TurnCompleted) and e.text == "FOREGROUND"
        )
        assert done.text == "FOREGROUND"
        assert ToolResult(done.turn_id, "tool-1", False, "WORK_DONE") in harness.events
        background = await harness.wait_for(_is_background)
        assert background.text == "BACKGROUND"
        assert not harness.session.background_work().items
    finally:
        await harness.stop()


async def test_foreground_waits_for_verified_tail_after_hub_notice(tmp_path: Path) -> None:
    harness = await start(tmp_path, extra_args=("--test-early-hub", "--test-multi-planner"))
    try:
        await harness.session.deliver([HarnessInput.of("timer", "background")])
        timer = await harness.wait_for(lambda e: isinstance(e, TurnCompleted))
        await harness.session.deliver([HarnessInput.of("first", "overlap-first")])
        await harness.wait_for(lambda e: isinstance(e, InputInContext) and e.key == "first")
        harness.release("release-foreground")
        first = await harness.wait_for(lambda e: isinstance(e, AssistantText) and e.text == "FIRST")
        harness.release("release-tool")
        await harness.wait_for(lambda e: isinstance(e, ToolResult) and e.tool_use_id == "timer")
        harness.release("release-front-result")
        await harness.wait_for_marker("front-result-sent")
        assert harness.session.active_turn_id == first.turn_id
        harness.release()
        done = await harness.wait_for(
            lambda e: (
                isinstance(e, TurnCompleted)
                and e.turn_id != timer.turn_id
                and e.text != "BACKGROUND"
            )
        )
        assert done.text == "FIRST\nTRUE FINAL" and done.num_turns == 2
        detached = await harness.wait_for(_is_background)
        assert detached.text == "BACKGROUND"
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
        harness.release("release-tool")
        await harness.wait_for(lambda e: isinstance(e, ToolResult) and e.tool_use_id == "timer")
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
        failure = await harness.wait_for(lambda e: isinstance(e, SessionFailed))
        assert "background reply timed out" in failure.error
        assert (
            sum(isinstance(e, TurnCompleted) and e.turn_id == done.turn_id for e in harness.events)
            == 1
        )
        assert not any(isinstance(e, UnsolicitedOutput) for e in harness.events)
    finally:
        await harness.stop()


async def test_stuck_background_reply_retires_before_sending_the_waiting_input(
    tmp_path: Path,
) -> None:
    harness = await start(tmp_path, reply_seconds=0.25)
    try:
        await harness.session.deliver([HarnessInput.of("timer", "background")])
        await harness.wait_for(lambda e: isinstance(e, TurnCompleted))
        harness.release("release-tool")
        await harness.wait_for(lambda e: isinstance(e, ToolResult) and e.tool_use_id == "timer")
        async with asyncio.timeout(2):
            with pytest.raises(InputsNotWrittenError, match="background reply") as failure:
                await harness.session.deliver([HarnessInput.of("next", "next")])
        assert failure.value.keys == {"next"}
        await harness.wait_for(lambda e: isinstance(e, SessionFailed))
        await harness.wait_for(lambda e: isinstance(e, InputRejected) and e.key == "next")
        assert await harness.session.reconcile(["next"]) == {"next": Reconciliation.NOT_FOUND}
        assert len(harness.prompts()) == 1
    finally:
        await harness.stop()


async def test_trajectory_keeps_reactive_idle_and_persists_thinking_duration(
    tmp_path: Path,
) -> None:
    harness = await start(tmp_path)
    try:
        fixture = Path(__file__).with_name("fixtures") / "native_thinking_timing.json"
        payload = json.loads(fixture.read_text())["reply"]
        payload["trajectory"]["steps"].append(
            {
                "type": "CORTEX_STEP_TYPE_RUN_COMMAND",
                "status": "CORTEX_STEP_STATUS_RUNNING",
            }
        )
        payload["status"] = "CASCADE_RUN_STATUS_IDLE"
        _ = (tmp_path / "trajectory.json").write_text(json.dumps(payload))
        path = (
            harness.home
            / ".gemini/antigravity-cli/brain/conv-acp/.system_generated/logs/transcript_full.jsonl"
        )
        _ = path.write_text(
            json.dumps(
                {
                    "type": "PLANNER_RESPONSE",
                    "status": "DONE",
                    "step_index": 0,
                    "created_at": "2026-11-17T05:30:00Z",
                    "thinking": "synthetic thought",
                }
            )
            + "\n"
        )
        await harness.session.deliver([HarnessInput.of("wait", "reactive")])
        reply = await harness.wait_for(lambda e: isinstance(e, AssistantText))
        assert reply.text == "Parked until the tool completes"
        assert harness.session.active_turn_id == reply.turn_id
        assert await harness.session.waiting_on_background([reply.turn_id])
        assert timing_path(path).is_file()
        assert "30265" in timing_path(path).read_text()
        assert not await harness.session.waiting_on_background(["another-turn"])
    finally:
        await harness.stop()


async def test_permission_rejection_is_a_failed_tool_and_turn(tmp_path: Path) -> None:
    harness = await start(tmp_path)
    try:
        await harness.session.deliver([HarnessInput.of("permission", "permission")])
        done = await harness.wait_for(lambda e: isinstance(e, TurnCompleted))
        assert done.is_error and done.permission_denials == ("Run command",)
        assert ToolResult(done.turn_id, "denied", True, "") in harness.events
    finally:
        await harness.stop()


async def test_jsonrpc_error_faults_only_its_turn(tmp_path: Path) -> None:
    harness = await start(tmp_path)
    try:
        await harness.session.deliver([HarnessInput.of("first", "broken")])
        fault = await harness.wait_for(lambda e: isinstance(e, TurnFaulted))
        assert "provider error" in fault.error
        await harness.session.deliver([HarnessInput.of("next", "next")])
        assert (await harness.wait_for(lambda e: isinstance(e, TurnCompleted))).text == "OK"
    finally:
        await harness.stop()


@pytest.mark.parametrize("cancel_while_blocked", [False, True])
async def test_large_steer_to_peer_that_stops_reading_retires_the_generation(
    tmp_path: Path, cancel_while_blocked: bool
) -> None:
    harness = await start(
        tmp_path,
        cancel_seconds=0.1,
        write_seconds=3.0 if cancel_while_blocked else 0.1,
        extra_args=("--test-child",),
    )
    try:
        await harness.session.deliver([HarnessInput.of("first", "hold-no-stdin")])
        tool = await harness.wait_for(lambda e: isinstance(e, ToolUse))
        image = ImageBlock("image/png", base64.b64encode(b"x" * (2 * 1024**2)).decode())
        async with asyncio.timeout(2), asyncio.TaskGroup() as tasks:

            async def send_batch() -> None:
                with pytest.raises(InputsNotWrittenError) as failure:
                    await harness.session.deliver(
                        [
                            HarnessInput.of("large", "steer", [image]),
                            HarnessInput.of("tail", "never written"),
                        ]
                    )
                assert failure.value.keys == {"tail"}

            sending = tasks.create_task(send_batch())
            await harness.wait_for(lambda e: isinstance(e, InputReceived) and e.key == "large")
            assert not sending.done()
            if cancel_while_blocked:
                report = await harness.session.interrupt(turn_id=tool.turn_id)
                assert report.cli_alive is False and report.settled is None
        failure = await harness.wait_for(lambda e: isinstance(e, SessionFailed))
        assert (
            "cancel unconfirmed" if cancel_while_blocked else "prompt write failed"
        ) in failure.error
        assert not any(
            isinstance(e, (InputRejected, InputLost)) and e.key == "large" for e in harness.events
        )
        await harness.wait_for(lambda e: isinstance(e, InputRejected) and e.key == "tail")
        await harness.stop()
        assert not PsutilChildProbe().is_alive(int((tmp_path / "child.pid").read_text()))
    finally:
        await harness.stop()


async def test_cancel_timeout_with_responsive_stdin_retires_the_generation(tmp_path: Path) -> None:
    harness = await start(
        tmp_path, cancel_seconds=0.1, extra_args=("--test-ignore-cancel", "--test-child")
    )
    try:
        await harness.session.deliver([HarnessInput.of("first", "hold")])
        tool = await harness.wait_for(lambda e: isinstance(e, ToolUse))
        report = await harness.session.interrupt(turn_id=tool.turn_id)
        assert report.cli_alive is False and report.settled is None
        assert harness.session.active_turn_id is None
        assert not PsutilChildProbe().is_alive(int((tmp_path / "child.pid").read_text()))
        await harness.wait_for_method("session/cancel")
        await harness.session.deliver([HarnessInput.of("next", "next")])
        reply = await harness.wait_for(lambda e: isinstance(e, TurnCompleted))
        assert reply.text == "OK" and reply.generation == 2
    finally:
        await harness.stop()


async def test_root_exit_with_inherited_stdout_does_not_strand_the_reader(tmp_path: Path) -> None:
    harness = await start(tmp_path, cancel_seconds=0.2, extra_args=("--test-child",))
    try:
        await harness.session.deliver([HarnessInput.of("first", "eof")])
        await harness.wait_for(lambda e: isinstance(e, SessionFailed))
        await harness.session.deliver([HarnessInput.of("next", "next")])
        done = await harness.wait_for(lambda e: isinstance(e, TurnCompleted))
        assert done.text == "OK" and done.generation == 2
        assert not PsutilChildProbe().is_alive(int((tmp_path / "child.pid").read_text()))
    finally:
        await harness.stop()


async def test_close_wakes_the_first_delivery_waiting_on_reopen(tmp_path: Path) -> None:
    harness = await start(tmp_path, extra_args=("--test-stall-load",), cancel_seconds=0.1)
    try:
        await harness.session.deliver([HarnessInput.of("eof", "eof")])
        await harness.wait_for(lambda e: isinstance(e, SessionFailed))
        sending = asyncio.create_task(harness.session.deliver([HarnessInput.of("reopen", "next")]))
        await harness.wait_for_method("session/load")
        assert not sending.done()
        await harness.stop()
        with pytest.raises(SessionClosedError, match="session is closed"):
            await asyncio.wait_for(sending, timeout=2)
        await harness.wait_for(lambda e: isinstance(e, InputRejected) and e.key == "reopen")
    finally:
        await harness.stop()


async def test_single_prompt_usage_wins_over_unrelated_and_incomplete_rows(tmp_path: Path) -> None:
    harness = await start(tmp_path)
    try:
        await harness.session.deliver([HarnessInput.of("metered", "noisy-usage")])
        done = await harness.wait_for(lambda e: isinstance(e, TurnCompleted))
        assert done.usage == Usage(40, 5, 0, 30) and done.steps == ()
    finally:
        await harness.stop()


def test_usage_handles_cumulative_steer_and_missing_cancel_usage() -> None:
    steps = (Usage(40, 3, 0, 30), Usage(65, 9, 0, 50))
    early = PromptUsage(10, 3, 30, 0, 2)
    cumulative = PromptUsage(25, 12, 80, 0, 7)
    complete = turn_usage((early, cumulative), steps, request_count=2, terminal_reported=True)
    assert complete.usage == Usage(105, 19, 0, 80) and not complete.uncertain
    for partial in (steps[:1], ()):
        incomplete = turn_usage(
            (early, cumulative), partial, request_count=2, terminal_reported=True
        )
        assert incomplete.usage == cumulative.billed() and incomplete.uncertain
    cancelled = turn_usage((early,), steps, request_count=2, terminal_reported=False)
    assert cancelled.usage == Usage(105, 14, 0, 80) and cancelled.uncertain
    single = turn_usage((cumulative,), steps[:1], request_count=1, terminal_reported=True)
    assert single.usage == cumulative.billed() and not single.uncertain


def test_planner_span_requires_own_inputs_and_final_row_without_background() -> None:
    rows: list[dict[str, object]] = [
        {"type": "USER_INPUT", "content": input_marker("one")},
        {
            "type": "PLANNER_RESPONSE",
            "status": "DONE",
            "input_tokens": 40,
            "output_tokens": 3,
            "content": "FIRST",
        },
        {"type": "USER_INPUT", "content": input_marker("two")},
        {
            "type": "PLANNER_RESPONSE",
            "status": "DONE",
            "input_tokens": 65,
            "output_tokens": 9,
            "content": "FINAL",
        },
    ]
    keys = ["one", "two"]
    assert planner_usage(rows, keys, "FINAL") == (Usage(40, 3), Usage(65, 9))
    assert planner_usage(rows[:-1], keys, "FINAL") == ()
    notice: dict[str, object] = {
        "type": "SYSTEM_MESSAGE",
        "content": "[Message] timestamp=2026-11-22T19:38:44Z sender=conv/task-12 priority=MESSAGE_PRIORITY_HIGH content=timer",
    }
    background = {**rows[-1], "content": "BACKGROUND", "input_tokens": 999}
    overlapping = [*rows[:2], notice, background, *rows[2:]]
    assert planner_usage(overlapping, keys, "FINAL") == (Usage(40, 3), Usage(65, 9))


def test_disjoint_steer_usage_survives_background_overlap_and_requires_first_planner() -> None:
    reports = (PromptUsage(2346, 89, 14735, 0, 36), PromptUsage(2317, 59, 15032, 0, 47))

    def planner(usage: PromptUsage, text: str) -> dict[str, object]:
        return {
            "type": "PLANNER_RESPONSE",
            "status": "DONE",
            "input_tokens": usage.input,
            "cache_read_tokens": usage.cached_read,
            "output_tokens": usage.output,
            "content": text,
        }

    first, final = planner(reports[0], "FIRST"), planner(reports[1], "FINAL")
    rows: list[dict[str, object]] = [
        {"type": "USER_INPUT", "content": input_marker("one")},
        first,
        {
            "type": "SYSTEM_MESSAGE",
            "content": "[Message] timestamp=2026-11-22T19:38:44Z sender=conv/task-12 priority=MESSAGE_PRIORITY_HIGH content=timer",
        },
        {**final, "content": "TIMER", "input_tokens": 20_000},
        {"type": "USER_INPUT", "content": input_marker("two")},
        final,
    ]
    steps = planner_usage(rows, ("one", "two"), "FINAL")
    bill = turn_usage(reports, steps, request_count=2, terminal_reported=True)
    assert bill.usage == Usage(34_430, 231, 0, 29_767) and not bill.uncertain
    billed = bill_prompt(rows, ("one", "two"), "FINAL", dict(enumerate(reports, 1)), 2)
    assert billed.calls == 2 and billed.usage == bill.usage and not billed.steps
    partial = planner_usage([rows[0], *rows[2:]], ("one", "two"), "FINAL")
    bill = turn_usage(reports, partial, request_count=2, terminal_reported=True)
    assert bill.usage == Usage(17_349, 106, 0, 15_032) and bill.uncertain


@pytest.mark.parametrize("answers", [("BACKGROUND",), ("BACKGROUND", "FOREGROUND")])
def test_turn_keeps_candidates_buffered_without_unique_ownership(answers: tuple[str, ...]) -> None:
    key = "one"
    ticket = ActiveTurn(turn_id="turn", key=key, blocks=())
    turn = PromptTurn(ticket, 0)
    turn.register_request(1, key)
    turn.stop_reason = "end_turn"
    turn.reports[1] = PromptUsage(10, 3, 0, 0, 0)
    projection = OutputProjection(0, keys=[key])
    rows: list[dict[str, object]] = [
        {"type": "USER_INPUT", "content": input_marker(key)},
        {
            "type": "SYSTEM_MESSAGE",
            "content": "[Message] timestamp=2026-11-22T19:38:44Z sender=conv/task-1 priority=MESSAGE_PRIORITY_HIGH content=timer",
        },
    ]
    for answer in answers:
        rows.append(
            {
                "type": "PLANNER_RESPONSE",
                "status": "DONE",
                "content": answer,
                "input_tokens": 10,
                "output_tokens": 3,
            }
        )
        projection.pending.append(
            {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": answer},
            }
        )
    turn.prove_projection(projection, rows)
    assert list(projection.project(rows)) == []
    assert len(projection.pending) == len(answers)
    assert not turn.projection_ready(projection, rows)


def _planner(text: str, inputs: int) -> dict[str, object]:
    return {
        "type": "PLANNER_RESPONSE",
        "status": "DONE",
        "content": text,
        "input_tokens": inputs,
        "output_tokens": 3,
    }


def _chunk(text: str) -> dict[str, object]:
    return {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": text}}


def test_foreground_total_must_cover_text_on_both_sides_of_the_hub_notice() -> None:
    key = "one"
    turn = PromptTurn(ActiveTurn(turn_id="turn", key=key, blocks=()), 0)
    turn.register_request(1, key)
    turn.stop_reason = "end_turn"
    turn.reports[1] = PromptUsage(30, 6, 0, 0, 0)
    projection = OutputProjection(0, keys=[key])
    rows: list[dict[str, object]] = [
        {"type": "USER_INPUT", "content": input_marker(key)},
        _planner("FIRST", 10),
        {
            "type": "SYSTEM_MESSAGE",
            "content": "[Message] timestamp=2026-11-22T19:38:44Z sender=conv/task-1 priority=MESSAGE_PRIORITY_HIGH content=timer",
        },
        _planner("TRUE FINAL", 20),
    ]
    projection.pending.extend((_chunk("FIRST"), _chunk("TRUE FINAL")))
    turn.prove_projection(projection, rows)
    [first] = list(projection.project(rows))
    assert first.input_key == key and first.frame == _chunk("FIRST")
    assert not turn.projection_ready(projection, rows)
    projection.close_foreground([key], len(rows), accounted=False)
    assert list(projection.project(rows)) == []
    rows.append(_planner("REAL BACKGROUND", 99))
    projection.pending.append(_chunk("REAL BACKGROUND"))
    turn.prove_projection(projection, rows)
    final, detached = projection.project(rows)
    assert final.input_key == key and final.frame == _chunk("TRUE FINAL")
    assert detached.native_notice("conv") and detached.reply == "REAL BACKGROUND"
    assert turn.projection_ready(projection, rows)
    result = turn.complete(rows, "end_turn", "conv", 0.0, 1)
    assert result.text == "FIRST\nTRUE FINAL" and result.usage == Usage(30, 6)


def test_repeated_text_consumes_earliest_row_for_the_same_owner() -> None:
    projection = OutputProjection(0, keys=["one"])
    rows: list[dict[str, object]] = [
        {"type": "USER_INPUT", "content": input_marker("one")},
        _planner("SAME", 10),
        _planner("SAME", 20),
    ]
    for index in (1, 2):
        projection.pending.append(_chunk("SAME"))
        [projected] = projection.project(rows)
        assert projected.input_key == "one" and projected.frame == _chunk("SAME")
        assert projection.seen == {(row, "content", 0) for row in range(1, index + 1)}
        assert not projection.pending


@pytest.mark.parametrize("first_planner_visible", [True, False])
def test_terminal_cumulative_steer_usage_without_early_receipt(
    first_planner_visible: bool,
) -> None:
    turn = PromptTurn(ActiveTurn(turn_id="turn", key="one", blocks=()), 0)
    turn.register_request(5, "one")
    turn.register_request(6, "two")
    turn.stop_reason = "end_turn"
    assert PromptUsage.from_result({"stopReason": "end_turn"}.get("usage")) is None
    receipt = PromptUsage.from_result(
        {
            "inputTokens": 6576,
            "outputTokens": 4268,
            "cachedReadTokens": 19166,
            "cachedWriteTokens": 0,
            "thoughtTokens": 343,
            "totalTokens": 30010,
        }
    )
    assert receipt is not None
    turn.reports[6] = receipt
    first = {**_planner("BEGIN_ALPHA", 2277), "cache_read_tokens": 8500, "output_tokens": 4100}
    final = {
        **_planner("DONE_ALPHA and STEER_BRAVO", 4299),
        "cache_read_tokens": 10666,
        "output_tokens": 168,
    }
    rows: list[dict[str, object]] = [
        {"type": "USER_INPUT", "content": input_marker("one")},
        *([first] if first_planner_visible else []),
        {"type": "USER_INPUT", "content": input_marker("two")},
        {
            "type": "SYSTEM_MESSAGE",
            "content": "[Message] timestamp=2026-11-22T19:38:44Z sender=conv/task-1 priority=MESSAGE_PRIORITY_HIGH content=timer",
        },
        final,
        _planner("BACKGROUND", 99),
    ]
    projection = OutputProjection(0, keys=["one", "two"])
    projection.pending.extend(
        _chunk(text) for text in ("BEGIN_ALPHA", "DONE_ALPHA and STEER_BRAVO", "BACKGROUND")
    )
    if not first_planner_visible:
        turn.reports[6] = PromptUsage(4299, 168, 10666, 0, 0)
        turn.prove_projection(projection, rows)
        assert not turn.projection_ready(projection, rows)
        assert list(projection.project(rows)) == []
        return
    turn.prove_projection(projection, rows)
    assert turn.projection_ready(projection, rows)
    events = list(projection.project(rows))
    assert [(event.input_key, event.reply) for event in events] == [
        ("one", ""),
        ("two", ""),
        (None, "BACKGROUND"),
    ]
    done = turn.complete(rows, "end_turn", "conv", 0.0, 1)
    assert done.text == "BEGIN_ALPHA\nDONE_ALPHA and STEER_BRAVO"
    assert done.usage == Usage(25_742, 4_611, 0, 19_166)
    assert done.num_turns == 2 and not done.usage_uncertain


def test_cancel_boundary_keeps_an_existing_planner_out_of_the_timer_reply() -> None:
    projection = OutputProjection(0, keys=["one"])
    interrupted = _planner("LATE FOREGROUND", 10)
    rows: list[dict[str, object]] = [
        {"type": "USER_INPUT", "content": input_marker("one")},
        {
            "type": "SYSTEM_MESSAGE",
            "content": "[Message] timestamp=2026-11-22T19:38:44Z sender=conv/task-1 priority=MESSAGE_PRIORITY_HIGH content=timer",
        },
        {**interrupted, "status": "RUNNING"},
    ]
    projection.close_foreground(["one"], len(rows), accounted=False, cancelled=True)
    rows[2] = interrupted
    rows.append(_planner("BACKGROUND", 99))
    projection.pending.extend((_chunk("LATE FOREGROUND"), _chunk("BACKGROUND")))
    [reply] = list(projection.project(rows))
    assert reply.reply == "BACKGROUND" and reply.native_notice("conv") is not None
    assert list(projection.pending) == [_chunk("LATE FOREGROUND")]


def test_command_completion_after_foreground_closes_keeps_native_ownership() -> None:
    key = "one"
    projection = OutputProjection(0, keys=[key])
    rows: list[dict[str, object]] = [
        {"type": "USER_INPUT", "content": input_marker(key)},
        {
            "step_index": 12,
            "source": "MODEL",
            "type": "GENERIC",
            "status": "RUNNING",
            "content": "Created At: 2026-11-22T19:38:44Z\nTool is running as a background task with task id: conv/task-12",
        },
    ]
    projection.close_foreground([key], len(rows), accounted=False)
    rows.extend(
        (
            {
                "type": "SYSTEM_MESSAGE",
                "content": '[Message] timestamp=2026-11-22T19:38:44Z sender=conv/task-12 priority=MESSAGE_PRIORITY_HIGH content=Task id "conv/task-12" finished with result:\nDone',
            },
            _planner("BACKGROUND", 99),
        )
    )
    projection.pending.append(_chunk("BACKGROUND"))
    [reply] = list(projection.project(rows))
    assert reply.input_key is None and reply.native_notice("conv") is not None

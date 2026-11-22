from __future__ import annotations

import json
from pathlib import Path

from base.lm.harness.antigravity.acp import input_marker
from base.lm.harness.antigravity.tests.support import start
from base.lm.harness.antigravity.timing_store import timing_path
from base.lm.harness.antigravity.usage import PromptUsage, turn_usage
from base.lm.harness.delivery import HarnessInput, ImageBlock, Reconciliation, TextBlock
from base.lm.harness.events import (
    AssistantText,
    InputInContext,
    InterruptConfirmed,
    ToolResult,
    ToolUse,
    TurnCompleted,
    TurnFaulted,
    UnsolicitedOutput,
    Usage,
)


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


async def test_cancel_keeps_process_and_next_turn_works(tmp_path: Path) -> None:
    harness = await start(tmp_path)
    try:
        pid = harness.session.cli_pid
        await harness.session.deliver([HarnessInput.of("hold", "hold")])
        tool = await harness.wait_for(lambda e: isinstance(e, ToolUse))
        report = await harness.session.interrupt(turn_id=tool.turn_id)
        assert report.cli_alive and report.cli_pid == pid and report.settled
        cancel = [frame for frame in harness.frames() if frame.get("method") == "session/cancel"]
        assert cancel == [
            {"jsonrpc": "2.0", "method": "session/cancel", "params": {"sessionId": "conv-acp"}}
        ]
        await harness.wait_for(lambda e: isinstance(e, InterruptConfirmed))
        await harness.session.deliver([HarnessInput.of("next", "next")])
        done = await harness.wait_for(
            lambda e: isinstance(e, TurnCompleted) and e.turn_id != tool.turn_id
        )
        assert done.text == "OK" and harness.session.cli_pid == pid
    finally:
        await harness.stop()


async def test_load_discards_replay_and_sets_model_and_effort_before_prompt(tmp_path: Path) -> None:
    sink: list[UnsolicitedOutput] = []
    harness = await start(tmp_path, resume="conv-acp", unsolicited=sink.append)
    try:
        await harness.session.deliver([HarnessInput.of("new", "new")])
        await harness.wait_for(lambda e: isinstance(e, TurnCompleted))
        frames = harness.frames()
        assert [f["method"] for f in frames] == [
            "initialize",
            "session/load",
            "session/set_model",
            "session/prompt",
        ]
        assert frames[2]["params"] == {"sessionId": "conv-acp", "modelId": "gemini-4-hydrogen-low"}
        assert sink == []
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
        harness.release()
        background = await harness.wait_for(lambda e: isinstance(e, UnsolicitedOutput))
        assert background.text == "BACKGROUND" and sink == [background]
        assert not harness.session.background_work().items
        assert ToolResult(done.turn_id, "timer", False, "") in harness.events
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


def test_usage_handles_cumulative_steer_and_missing_cancel_usage() -> None:
    steps = (Usage(40, 3, 0, 30), Usage(65, 9, 0, 50))
    early = PromptUsage(10, 3, 30, 0, 2)
    cumulative = PromptUsage(25, 12, 80, 0, 7)
    assert turn_usage((early, cumulative), steps) == Usage(105, 19, 0, 80)
    assert turn_usage((cumulative,), steps) == Usage(105, 19, 0, 80)
    assert turn_usage((early,), steps) == Usage(105, 14, 0, 80)
    assert turn_usage((), steps) == Usage(105, 12, 0, 80)

from __future__ import annotations

from base.lm.harness.codex.mapping import (
    approval_denial,
    assistant_text,
    is_final_answer,
    item_event,
    parse_turn,
    text_delta,
    usage_from,
)
from base.lm.harness.events import TextDelta, ToolResult, ToolUse


def test_delta_maps_to_text_delta() -> None:
    assert text_delta({"delta": "hi"}, "turn-1") == [TextDelta(turn_id="turn-1", text="hi")]
    assert text_delta({"delta": ""}, "turn-1") == []


def test_command_item_maps_to_one_tool_use_and_one_result() -> None:
    started = item_event(
        {
            "item": {
                "type": "commandExecution",
                "id": "exec-1",
                "command": "echo hi",
                "cwd": "workspace",
                "status": "inProgress",
            }
        },
        phase="started",
        turn_id="turn-1",
    )
    assert started == [
        ToolUse(
            turn_id="turn-1",
            tool_use_id="exec-1",
            name="command",
            tool_input={"command": "echo hi", "cwd": "workspace"},
        )
    ]
    completed = item_event(
        {
            "item": {
                "type": "commandExecution",
                "id": "exec-1",
                "status": "completed",
                "exitCode": 2,
                "aggregatedOutput": "boom",
            }
        },
        phase="completed",
        turn_id="turn-1",
    )
    assert completed == [
        ToolResult(turn_id="turn-1", tool_use_id="exec-1", is_error=True, content="boom")
    ]


def test_declined_command_is_an_error_result() -> None:
    events = item_event(
        {"item": {"type": "commandExecution", "id": "exec-1", "status": "declined"}},
        phase="completed",
        turn_id="turn-1",
    )
    assert isinstance(events[0], ToolResult)
    assert events[0].is_error is True


def test_file_change_maps_to_a_patch_tool() -> None:
    events = item_event(
        {
            "item": {
                "type": "fileChange",
                "id": "change-1",
                "status": "completed",
                "changes": [{"path": "a.py"}, {"path": "b.py"}],
            }
        },
        phase="started",
        turn_id="turn-1",
    )
    assert isinstance(events[0], ToolUse)
    assert events[0].name == "patch"
    assert events[0].tool_input == {"paths": ["a.py", "b.py"]}


def test_unknown_item_maps_to_nothing() -> None:
    assert (
        item_event({"item": {"type": "reasoning", "id": "r-1"}}, phase="completed", turn_id="t")
        == []
    )


def test_assistant_text_and_phase() -> None:
    item = {"type": "agentMessage", "text": "hello", "phase": "final_answer"}
    assert assistant_text(item) == "hello"
    assert is_final_answer(item) is True
    assert is_final_answer({"type": "agentMessage", "phase": "commentary"}) is False


def test_usage_reads_the_last_turn_breakdown() -> None:
    usage = usage_from(
        {
            "tokenUsage": {
                "last": {
                    "inputTokens": 12,
                    "cachedInputTokens": 3,
                    "cacheWriteInputTokens": 1,
                    "outputTokens": 4,
                }
            }
        }
    )
    assert usage.input_tokens == 12
    assert usage.cache_read_input_tokens == 3
    assert usage.cache_creation_input_tokens == 1
    assert usage.output_tokens == 4


def test_parse_turn_carries_the_error_message() -> None:
    fields = parse_turn({"status": "failed", "error": {"message": "overloaded"}, "durationMs": 90})
    assert fields.status == "failed"
    assert fields.error_message == "overloaded"
    assert fields.duration_ms == 90


def test_approval_denial_names_the_command() -> None:
    assert (
        approval_denial("item/commandExecution/requestApproval", {"command": "ls"}) == "command: ls"
    )
    assert approval_denial("item/fileChange/requestApproval", {}) == "file change"

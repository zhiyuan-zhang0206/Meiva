from __future__ import annotations

from base.lm.claude_sdk.events import TextDelta, ToolResult, ToolUse
from base.lm.harness.antigravity.mapping import (
    assistant_text,
    map_init,
    map_step_update,
    parse_result,
    parse_usage,
)


def test_init_carries_the_conversation_and_permission_mode() -> None:
    event = map_init(
        {
            "event": "init",
            "conversation_id": "conv-1",
            "init": {
                "model": "gemini-4-argon-high",
                "cwd": "/work",
                "permission_mode": "always-proceed",
            },
        }
    )
    assert event.session_id == "conv-1"
    assert event.model == "gemini-4-argon-high"
    assert event.cwd == "/work"
    assert event.api_key_source == "always-proceed"


def test_thinking_tokens_are_folded_into_output() -> None:
    usage = parse_usage(
        {"input_tokens": 11, "output_tokens": 2, "thinking_tokens": 7, "cache_read_tokens": 3}
    )
    assert usage.input_tokens == 11
    assert usage.output_tokens == 9
    assert usage.cache_read_input_tokens == 3


def test_denied_actions_turn_a_successful_result_into_a_failure() -> None:
    fields = parse_result(
        {
            "conversation_id": "conv-1",
            "status": "SUCCESS",
            "response": "",
            "num_turns": 1,
            "duration_seconds": 1.5,
            "denied_actions": [{"action": "command", "display_name": "RunCommand"}],
        }
    )
    assert fields.status == "SUCCESS"
    assert fields.denied is True
    assert fields.is_error is True
    assert fields.subtype == "denied"
    assert fields.denied_actions == ("command:RunCommand",)
    assert fields.duration_ms == 1500


def test_a_plain_success_is_not_an_error() -> None:
    fields = parse_result({"status": "SUCCESS", "response": "ok"})
    assert fields.is_error is False
    assert fields.subtype == "success"
    assert fields.denied_actions == ()


def test_interrupted_result_is_flagged() -> None:
    fields = parse_result({"status": "ERROR", "error": "interrupted"})
    assert fields.interrupted is True
    assert fields.is_error is True


def test_tool_call_and_result_share_one_id() -> None:
    active = {
        "step_index": 2,
        "state": "ACTIVE",
        "step_type": "tool",
        "tool_name": "run_command",
        "tool_info": {"name": "run_command", "parameters": {"CommandLine": "ls"}},
    }
    done = {
        "step_index": 2,
        "state": "DONE",
        "step_type": "tool",
        "tool_name": "run_command",
        "tool_info": {
            "name": "run_command",
            "parameters": {"CommandLine": "ls"},
            "output": "a.txt",
        },
    }
    use = map_step_update(active, turn_id="turn-1")
    result = map_step_update(done, turn_id="turn-1")
    assert isinstance(use[0], ToolUse)
    assert isinstance(result[0], ToolResult)
    assert use[0].tool_use_id == result[0].tool_use_id
    assert use[0].tool_input == {"CommandLine": "ls"}
    assert result[0].content == "a.txt"
    assert result[0].is_error is False


def test_text_delta_and_assistant_text() -> None:
    update = {"step_index": 1, "state": "ACTIVE", "step_type": "agent_response", "text_delta": "hi"}
    events = map_step_update(update, turn_id="turn-1")
    assert isinstance(events[0], TextDelta)
    assert assistant_text(update, turn_id="turn-1", text="") is None
    done = {"step_index": 1, "state": "DONE", "step_type": "agent_response", "text_delta": ""}
    assert assistant_text(done, turn_id="turn-1", text="hi") is not None

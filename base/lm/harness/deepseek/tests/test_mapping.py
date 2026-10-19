from __future__ import annotations

from base.lm.harness.deepseek.mapping import (
    TOOL_RESULT_PREVIEW_CHARS,
    assistant_text,
    map_update,
    parse_prompt_result,
)
from base.lm.harness.events import AssistantText, TextDelta, ToolResult, ToolUse


def chunk(text: str) -> dict[str, object]:
    return {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": text}}


def test_a_message_chunk_is_one_text_delta() -> None:
    [event] = map_update(chunk("hello there"), turn_id="turn-1")
    assert event == TextDelta(turn_id="turn-1", text="hello there")


def test_an_empty_chunk_produces_no_event() -> None:
    assert map_update(chunk(""), turn_id="turn-1") == []


def test_a_thought_chunk_is_not_output() -> None:
    update = {"sessionUpdate": "agent_thought_chunk", "content": {"type": "text", "text": "hm"}}
    assert map_update(update, turn_id="turn-1") == []


def test_an_unknown_update_is_ignored() -> None:
    assert map_update({"sessionUpdate": "plan"}, turn_id="turn-1") == []


def test_a_tool_call_maps_to_a_tool_use() -> None:
    update = {
        "sessionUpdate": "tool_call",
        "toolCallId": "call-1",
        "title": "read file",
        "kind": "read",
        "rawInput": {"path": "a.txt"},
    }
    [event] = map_update(update, turn_id="turn-1")
    assert isinstance(event, ToolUse)
    assert event.tool_use_id == "call-1"
    assert event.name == "read file"
    assert event.tool_input == {"path": "a.txt"}


def test_a_tool_call_without_a_title_falls_back_to_its_kind() -> None:
    [event] = map_update(
        {"sessionUpdate": "tool_call", "toolCallId": "call-1", "kind": "execute"},
        turn_id="turn-1",
    )
    assert isinstance(event, ToolUse)
    assert event.name == "execute"
    assert event.tool_input == {}


def test_an_unsettled_tool_call_update_is_not_a_result() -> None:
    update = {"sessionUpdate": "tool_call_update", "toolCallId": "call-1", "status": "in_progress"}
    assert map_update(update, turn_id="turn-1") == []


def test_a_completed_tool_call_update_maps_to_a_result() -> None:
    update = {
        "sessionUpdate": "tool_call_update",
        "toolCallId": "call-1",
        "status": "completed",
        "content": [
            {"type": "content", "content": {"type": "text", "text": "line one"}},
            {"type": "content", "content": {"type": "text", "text": "line two"}},
        ],
    }
    [event] = map_update(update, turn_id="turn-1")
    assert isinstance(event, ToolResult)
    assert event.tool_use_id == "call-1"
    assert event.is_error is False
    assert event.content == "line one\nline two"


def test_a_failed_tool_call_update_is_an_error_result() -> None:
    update = {"sessionUpdate": "tool_call_update", "toolCallId": "call-1", "status": "failed"}
    [event] = map_update(update, turn_id="turn-1")
    assert isinstance(event, ToolResult)
    assert event.is_error is True
    assert event.content == ""


def test_a_large_tool_result_is_previewed() -> None:
    body = "x" * (TOOL_RESULT_PREVIEW_CHARS + 50)
    update = {
        "sessionUpdate": "tool_call_update",
        "toolCallId": "call-1",
        "status": "completed",
        "content": [{"type": "content", "content": {"type": "text", "text": body}}],
    }
    [event] = map_update(update, turn_id="turn-1")
    assert isinstance(event, ToolResult)
    assert event.content.startswith("x" * TOOL_RESULT_PREVIEW_CHARS)
    assert event.content.endswith("(+50 chars)")


def test_a_trimmed_answer_becomes_the_turn_text() -> None:
    assert assistant_text("  done  ", turn_id="turn-1") == AssistantText(
        turn_id="turn-1", text="done"
    )
    assert assistant_text("   ", turn_id="turn-1") is None


def test_each_stop_reason_maps_to_its_subtype() -> None:
    assert parse_prompt_result({"stopReason": "end_turn"}).subtype == "success"
    cancelled = parse_prompt_result({"stopReason": "cancelled"})
    assert cancelled.subtype == "cancelled"
    assert cancelled.interrupted is True
    assert cancelled.is_error is True
    for reason in ("max_tokens", "refusal", "max_turn_requests"):
        fields = parse_prompt_result({"stopReason": reason})
        assert fields.subtype == reason
        assert fields.is_error is True


def test_an_unknown_stop_reason_is_a_failure_not_a_success() -> None:
    fields = parse_prompt_result({"stopReason": "something_new"})
    assert fields.stop_reason == "something_new"
    assert fields.subtype == "something_new"
    assert fields.is_error is True


def test_a_missing_stop_reason_is_unknown() -> None:
    fields = parse_prompt_result({})
    assert fields.stop_reason == "unknown"
    assert fields.is_error is True

from __future__ import annotations

from base.lm.harness.claude.mapping import TOOL_RESULT_PREVIEW_CHARS, map_message, parse_result
from base.lm.harness.claude.tests import fake_sdk as fake
from base.lm.harness.events import (
    AssistantText,
    RateLimitObserved,
    SessionStarted,
    TextDelta,
    ToolResult,
    ToolUse,
    Usage,
)


def test_text_delta_maps_to_delta_event() -> None:
    message = fake.StreamEvent(
        event={"type": "content_block_delta", "delta": {"type": "text_delta", "text": "bro"}}
    )
    assert map_message(message, turn_id="t1") == [TextDelta(turn_id="t1", text="bro")]


def test_stream_events_that_are_not_text_deltas_are_ignored() -> None:
    thinking = fake.StreamEvent(
        event={"type": "content_block_delta", "delta": {"type": "thinking_delta", "thinking": "x"}}
    )
    start = fake.StreamEvent(event={"type": "message_start"})
    assert map_message(thinking, turn_id="t1") == []
    assert map_message(start, turn_id="t1") == []


def test_assistant_blocks_map_to_text_and_tool_use() -> None:
    message = fake.AssistantMessage(
        content=[
            fake.TextBlock(text="reading it"),
            fake.ToolUseBlock(id="toolu-9", name="Read", input={"file_path": "hello.txt"}),
        ]
    )
    assert map_message(message, turn_id="t1") == [
        AssistantText(turn_id="t1", text="reading it"),
        ToolUse(
            turn_id="t1", tool_use_id="toolu-9", name="Read", tool_input={"file_path": "hello.txt"}
        ),
    ]


def test_tool_result_maps_with_error_flag() -> None:
    message = fake.UserMessage(
        content=[fake.ToolResultBlock(tool_use_id="toolu-9", content="denied", is_error=True)]
    )
    assert map_message(message, turn_id="t1") == [
        ToolResult(turn_id="t1", tool_use_id="toolu-9", is_error=True, content="denied")
    ]


def test_tool_result_is_clipped_for_the_timeline() -> None:
    message = fake.UserMessage(
        content=[fake.ToolResultBlock(tool_use_id="toolu-9", content="x" * 5000)]
    )
    [event] = map_message(message, turn_id="t1")
    assert isinstance(event, ToolResult)
    assert len(event.content) == TOOL_RESULT_PREVIEW_CHARS + len("...")


def test_init_message_maps_to_session_started() -> None:
    message = fake.SystemMessage(
        subtype="init",
        data={
            "session_id": "sess-1",
            "model": "claude-sonnet-5-5",
            "cwd": "/workspace/agent",
            "apiKeySource": "none",
        },
    )
    assert map_message(message, turn_id=None) == [
        SessionStarted(
            session_id="sess-1",
            model="claude-sonnet-5-5",
            cwd="/workspace/agent",
            api_key_source="none",
        )
    ]


def test_rate_limit_maps_to_quota_signal() -> None:
    message = fake.RateLimitEvent(
        rate_limit_info=fake.RateLimitInfo(
            status="allowed_warning",
            utilization=0.96,
            resets_at=1795000000,
            rate_limit_type="seven_day",
        )
    )
    assert map_message(message, turn_id=None) == [
        RateLimitObserved(
            status="allowed_warning",
            utilization=0.96,
            resets_at=1795000000,
            rate_limit_type="seven_day",
        )
    ]


def test_result_fields_carry_usage_cost_and_denials() -> None:
    fields = parse_result(
        fake.ResultMessage(
            subtype="success",
            session_id="sess-1",
            total_cost_usd=0.0004,
            permission_denials=[{"tool_name": "Write"}],
        )
    )
    assert fields.session_id == "sess-1"
    assert fields.usage == Usage(
        input_tokens=4,
        output_tokens=138,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=2738,
    )
    assert fields.cost_usd == 0.0004
    assert fields.permission_denials == ("Write",)


def test_unknown_messages_map_to_nothing() -> None:

    class FutureMessage:
        pass

    assert map_message(FutureMessage(), turn_id="t1") == []

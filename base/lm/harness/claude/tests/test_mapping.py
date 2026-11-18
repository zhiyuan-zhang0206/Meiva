from __future__ import annotations

from base.lm.harness.claude.mapping import (
    AssistantError,
    map_message,
    parse_assistant_error,
    parse_result,
)
from base.lm.harness.claude.tests import fake_sdk as fake
from base.lm.harness.events import (
    AssistantText,
    ContextCompacted,
    RateLimitObserved,
    ReasoningDelta,
    SessionStarted,
    TextDelta,
    ToolResult,
    ToolUse,
    UnsolicitedOutput,
    Usage,
)


def test_text_delta_maps_to_delta_event() -> None:
    message = fake.StreamEvent(
        event={"type": "content_block_delta", "delta": {"type": "text_delta", "text": "bro"}}
    )
    assert map_message(message, turn_id="t1") == [TextDelta(turn_id="t1", text="bro")]


def test_thinking_delta_maps_and_unrelated_stream_events_are_ignored() -> None:
    thinking = fake.StreamEvent(
        event={"type": "content_block_delta", "delta": {"type": "thinking_delta", "thinking": "x"}}
    )
    start = fake.StreamEvent(event={"type": "message_start"})
    assert map_message(thinking, turn_id="t1") == [ReasoningDelta(turn_id="t1", text="x")]
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


def test_tool_result_preserves_full_untruncated_content() -> None:
    full_output = "x" * 5000
    message = fake.UserMessage(
        content=[fake.ToolResultBlock(tool_use_id="toolu-9", content=full_output)]
    )
    [event] = map_message(message, turn_id="t1")
    assert isinstance(event, ToolResult)
    assert event.content == full_output


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
        input_tokens=4 + 2738,
        output_tokens=138,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=2738,
    )
    assert fields.cost_usd == 0.0004
    assert fields.permission_denials == ("Write",)


# meiva:note
# The numbers are a real turn recorded on 2026-10-20: Anthropic billed 8 uncached
# input tokens beside 17391 cache reads and 6621 cache writes. Reading the remainder
# as the prompt priced a 24020-token turn as 8 tokens and raised in `quote`, which
# deleted the reply the turn had already produced.
# meiva:link base/lm/harness/docs/harness.md
def test_usage_reports_the_whole_prompt_not_only_the_uncached_remainder() -> None:
    fields = parse_result(
        fake.ResultMessage(
            subtype="success",
            session_id="sess-2",
            usage={
                "input_tokens": 8,
                "output_tokens": 371,
                "cache_creation_input_tokens": 6621,
                "cache_read_input_tokens": 17391,
            },
        )
    )
    assert fields.usage.input_tokens == 8 + 6621 + 17391
    cached = fields.usage.cache_read_input_tokens + fields.usage.cache_creation_input_tokens
    assert cached <= fields.usage.input_tokens, "the cache fields are subsets of the prompt"


# meiva:note
# The failed-API-call shape: `is_error=True` with `subtype="success"`, the status in
# `api_error_status` and the provider's sentence in the result text (#6150). The fields have to
# survive the mapping or the bridge can only report the subtype.
def test_result_fields_carry_the_api_error_status_and_the_error_list() -> None:
    fields = parse_result(
        fake.ResultMessage(
            subtype="success",
            is_error=True,
            stop_reason="stop_sequence",
            result="API Error: 400 This API key is not scoped to a workspace",
            api_error_status=400,
            errors=["bad request"],
        )
    )

    assert fields.is_error is True
    assert fields.api_error_status == 400
    assert fields.errors == ("bad request",)
    assert fields.result_text == "API Error: 400 This API key is not scoped to a workspace"


# meiva:note
# On that shape the CLI may send no error list at all and put the provider's sentence in `result`
# instead -- the only words anyone has about the failure. Declared as an error here, it reaches
# the fault message; left in `result`, only the turn's reply text was left to quote (#6498).
def test_a_failed_api_call_without_an_error_list_declares_the_result_text() -> None:
    fields = parse_result(
        fake.ResultMessage(
            subtype="success",
            is_error=True,
            stop_reason="stop_sequence",
            result="API Error: 400 This API key is not scoped to a workspace",
            api_error_status=400,
        )
    )

    assert fields.errors == ("API Error: 400 This API key is not scoped to a workspace",)


# meiva:note
# Without an API status there is nothing that says `result` is about the failure rather than the
# reply, so a failed turn declares no error text and the fault message says it has no detail.
def test_a_failed_result_without_an_api_status_declares_no_error_text() -> None:
    fields = parse_result(
        fake.ResultMessage(subtype="error_max_turns", is_error=True, result="assistant prose")
    )

    assert fields.errors == ()
    assert fields.result_text == "assistant prose"


def test_a_successful_result_carries_no_status_and_no_errors() -> None:
    fields = parse_result(fake.ResultMessage(subtype="success"))

    assert fields.api_error_status is None
    assert fields.errors == ()


def test_unknown_messages_map_to_nothing() -> None:

    class FutureMessage:
        pass

    assert map_message(FutureMessage(), turn_id="t1") == []


def test_a_finished_background_task_maps_to_unsolicited_output() -> None:
    # meiva:note
    # The report the CLI injects when a background command ends. It belongs to no
    # turn of ours, so it maps to unsolicited output carrying what the agent needs to
    # read — the task, its status, and where the output went.
    # meiva:link base/lm/harness/claude/docs/claude.md
    message = fake.TaskNotificationMessage(
        subtype="task_notification",
        data={},
        task_id="bhd09i69m",
        status="completed",
        output_file="/var/tasks/bhd09i69m.output",
        summary='Background command "sleep 100; echo BGDONE" completed (exit code 0)',
    )

    [event] = map_message(message, turn_id=None)

    assert event == UnsolicitedOutput(
        source="task-notification",
        text=(
            "Background task bhd09i69m reported completed.\n"
            'Background command "sleep 100; echo BGDONE" completed (exit code 0)\n'
            "Output file: /var/tasks/bhd09i69m.output"
        ),
    )


def test_a_task_report_without_a_summary_still_names_the_output() -> None:
    [event] = map_message(
        fake.TaskNotificationMessage(subtype="task_notification", data={}, task_id="t1"),
        turn_id=None,
    )

    assert isinstance(event, UnsolicitedOutput)
    assert "Background task t1 reported completed." in event.text
    assert "Output file" not in event.text


def test_a_result_carries_the_origin_the_cli_names() -> None:
    # meiva:note
    # The origin is what lets the session tell its own turn from the CLI's turn over
    # a notification; a CLI that names nothing answers None and is read as ours.
    # meiva:link base/lm/harness/claude/docs/claude.md
    own = parse_result(fake.ResultMessage(subtype="success"))
    foreign = parse_result(
        fake.ResultMessage(subtype="success", origin={"kind": "task-notification"})
    )
    human = parse_result(fake.ResultMessage(subtype="success", origin={"kind": "human"}))

    assert own.origin_kind is None
    assert foreign.origin_kind == "task-notification"
    assert human.origin_kind == "human"


def test_a_compact_boundary_system_message_maps_to_context_compacted() -> None:
    message = fake.SystemMessage(subtype="compact_boundary", data={"compact_metadata": {}})
    assert map_message(message, turn_id=None) == [ContextCompacted(turn_id=None)]
    status = fake.SystemMessage(subtype="status", data={"status": "compacting"})
    assert map_message(status, turn_id=None) == []


def test_synthetic_auth_error_assistant_frame_is_extracted_and_excluded_from_assistant_text() -> (
    None
):
    auth_text = (
        "Failed to authenticate: this login reached its time limit and cannot be renewed. "
        "Run `claude auth login` to sign in again..."
    )
    sdk_shape = fake.AssistantMessage(
        model="<synthetic>",
        error="authentication_failed",
        stop_reason="stop_sequence",
        content=[fake.TextBlock(text=auth_text)],
    )
    raw_shape = fake.AssistantMessage(
        model="<synthetic>",
        error="authentication_failed",
        stop_reason="stop_sequence",
        isApiErrorMessage=True,
        api_error_status=401,
        content=[fake.TextBlock(text=auth_text)],
    )

    assert parse_assistant_error(sdk_shape) == AssistantError(
        error="authentication_failed",
        text=auth_text,
        api_error_status=None,
    )
    assert parse_assistant_error(raw_shape) == AssistantError(
        error="authentication_failed",
        text=auth_text,
        api_error_status=401,
    )
    assert map_message(sdk_shape, turn_id="t1") == []
    assert map_message(raw_shape, turn_id="t1") == []


def test_non_error_synthetic_assistant_frame_is_ignored_without_error() -> None:
    synthetic_ack = fake.AssistantMessage(
        model="<synthetic>",
        stop_reason="stop_sequence",
        isApiErrorMessage=False,
        content=[fake.TextBlock(text="No response requested.")],
    )

    assert parse_assistant_error(synthetic_ack) is None
    assert map_message(synthetic_ack, turn_id="t1") == []


def test_assistant_error_redacts_secrets_while_keeping_diagnostic_message() -> None:
    leaked = (
        "Failed to authenticate Authorization: Bearer tok_live_secret_123 "
        "https://user:supersecret@api.anthropic.com "
        "CLAUDE_CODE_OAUTH_TOKEN=fixture-secret-value "
        "sk-ant-oat01-fixturesecret"
    )
    message = fake.AssistantMessage(
        model="<synthetic>",
        error="authentication_failed",
        isApiErrorMessage=True,
        content=[fake.TextBlock(text=leaked)],
    )

    parsed = parse_assistant_error(message)
    assert parsed is not None
    assert parsed.error == "authentication_failed"
    assert parsed.text is not None
    assert "supersecret" not in parsed.text
    assert "fixture-secret-value" not in parsed.text
    assert "sk-ant-oat01-fixturesecret" not in parsed.text
    assert "tok_live_secret_123" not in parsed.text
    assert "Failed to authenticate" in parsed.text

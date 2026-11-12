# meiva:note
# Rows of a Codex rollout that a statistic over production rollouts (CLI 0.163 to 0.166; only row kinds, key names and value
# types were counted, no content) showed and that no recording of ours holds. They are built here from those shapes and are
# not recordings: the values are invented, the keys and types are the observed ones. Real rows were made where the CLI
# could be made to write them cheaply (none of these could: the model has no sleep tool, and no MCP server or second
# agent is configured).
# meiva:link base/lm/harness/codex/docs/rollout.md

from __future__ import annotations

from base.lm.harness.codex.transcript.items import RESULT_ITEM_CHARS
from base.lm.harness.codex.transcript.parse import parse_rollout_line
from base.lm.harness.session.transcript.entries import (
    MAIN_RECORD,
    Notice,
    ToolCall,
    ToolResult,
    ToolStatus,
)

TURN = "turn-1"


def _line(kind: str, payload: dict[str, object], ordinal: int = 7) -> dict[str, object]:
    return {
        "timestamp": "2026-11-12T19:45:25.361Z",
        "ordinal": ordinal,
        "type": kind,
        "payload": payload,
    }


def _item(item: dict[str, object]) -> dict[str, object]:
    return _line(
        "event_msg",
        {
            "type": "item_completed",
            "turn_id": TURN,
            "item": item,
            "started_at_ms": 1000,
            "completed_at_ms": 1250,
        },
    )


def test_rows_that_only_carry_the_agents_own_bookkeeping_are_skipped_without_a_report() -> None:
    quiet = [
        _line("inter_agent_communication_metadata", {"trigger_turn": True}),
        _line(
            "response_item",
            {
                "type": "agent_message",
                "id": "m1",
                "author": "a",
                "recipient": "b",
                "content": [{"type": "encrypted_content", "encrypted_content": "x"}],
            },
        ),
        _line("response_item", {"type": "configuration_update", "reasoning": {"effort": "low"}}),
    ]
    assert [parse_rollout_line(row, record=MAIN_RECORD) for row in quiet] == [[], [], []]


def test_a_sub_agent_start_and_end_are_status_markers_and_a_message_to_it_is_nothing() -> None:
    def activity(kind: str) -> list[object]:
        return list(
            parse_rollout_line(
                _item(
                    {
                        "type": "SubAgentActivity",
                        "id": f"sa-{kind}",
                        "kind": kind,
                        "agent_path": "/root/helper",
                        "agent_thread_id": "thread-2",
                        "model": "m",
                        "reasoning_effort": "low",
                    }
                ),
                record=MAIN_RECORD,
            )
        )

    (started,) = activity("started")
    (completed,) = activity("completed")
    assert isinstance(started, Notice) and isinstance(completed, Notice)
    assert (started.text, completed.text) == (
        "Sub-agent /root/helper started",
        "Sub-agent /root/helper completed",
    )
    assert not started.opens_turn and not completed.opens_turn
    assert activity("interacted") == []


def _mcp(**fields: object) -> dict[str, object]:
    return _item(
        {
            "type": "McpToolCall",
            "id": "mcp-1",
            "server": "docs",
            "tool": "lookup",
            "arguments": {"query": "q"},
            "status": "completed",
            "duration": {"secs": 0, "nanos": 5_000_000},
            "appName": "docs",
            "actionName": "lookup",
            **fields,
        }
    )


def test_an_mcp_call_is_a_call_with_its_arguments_and_the_text_of_its_result() -> None:
    result = {
        "content": [{"type": "text", "text": "line one"}, {"type": "image", "data": "A" * 5000}],
        "isError": False,
        "structuredContent": {"k": "v"},
    }
    call, answer = parse_rollout_line(_mcp(result=result), record=MAIN_RECORD)
    assert isinstance(call, ToolCall) and isinstance(answer, ToolResult)
    assert (call.name, dict(call.tool_input)) == ("mcp:docs/lookup", {"query": "q"})
    assert answer.content == "line one\n[image]" and answer.status is ToolStatus.OK
    assert "AAAA" not in answer.content


def test_an_mcp_call_that_failed_or_reports_an_error_is_an_error_and_a_long_result_is_cut() -> None:
    (_, failed) = parse_rollout_line(
        _mcp(status="failed", result={"content": [], "isError": False}), record=MAIN_RECORD
    )
    (_, flagged) = parse_rollout_line(
        _mcp(result={"content": [{"type": "text", "text": "x" * 10_000}], "isError": True}),
        record=MAIN_RECORD,
    )
    assert isinstance(failed, ToolResult) and failed.status is ToolStatus.ERROR
    assert isinstance(flagged, ToolResult) and flagged.status is ToolStatus.ERROR
    assert len(flagged.content) == RESULT_ITEM_CHARS


def test_a_clock_sleep_is_a_call_named_sleep_with_its_duration() -> None:
    call, answer = parse_rollout_line(
        _item({"type": "Extension", "id": "sl-1", "kind": "clock.sleep", "durationMs": 1500}),
        record=MAIN_RECORD,
    )
    assert isinstance(call, ToolCall) and isinstance(answer, ToolResult)
    assert (call.name, dict(call.tool_input)) == ("sleep", {"durationMs": 1500})
    assert answer.status is ToolStatus.OK

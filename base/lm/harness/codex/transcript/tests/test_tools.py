from __future__ import annotations

from collections.abc import Mapping

from base.lm.harness.codex.transcript.parse import parse_item_row, parse_rollout_line
from base.lm.harness.session.transcript.entries import (
    MAIN_RECORD,
    AgentText,
    Anomaly,
    AnomalyKind,
    AnyEntry,
    Failure,
    Notice,
    Reasoning,
    ToolCall,
    ToolResult,
    ToolStatus,
)

TURN = "turn-1"


def _row(item: Mapping[str, object], *, done: bool = True) -> dict[str, object]:
    item = {"id": "it-1", "startedAtMs": 1_000, **item}
    if done:
        item["completedAtMs"] = 1_250
    return {"turnId": TURN, "item": item, "startedAtMs": 1_000, "completedAtMs": 1_250}


def _read(item: Mapping[str, object], *, done: bool = True) -> list[AnyEntry]:
    return parse_item_row(_row(item, done=done), 7, MAIN_RECORD, set())


def _call_and_result(entries: list[AnyEntry]) -> tuple[ToolCall, ToolResult]:
    call, result = entries
    assert isinstance(call, ToolCall) and isinstance(result, ToolResult)
    assert call.call_id == result.call_id == "it-1" and call.turn == result.turn == TURN
    return call, result


def test_a_file_change_is_a_patch_call_with_its_changes_and_a_result_of_its_status() -> None:
    changes = [
        {"path": "a.py", "kind": {"type": "update", "move_path": None}, "diff": "@@ -1 +1 @@"},
        {"path": "b.py", "kind": {"type": "add"}, "diff": "+new"},
    ]
    call, result = _call_and_result(
        _read({"type": "fileChange", "changes": changes, "status": "completed"})
    )
    assert call.name == "patch"
    assert call.tool_input["paths"] == ["a.py", "b.py"]
    assert call.tool_input["changes"] == [
        {"path": "a.py", "kind": "update", "diff": "@@ -1 +1 @@"},
        {"path": "b.py", "kind": "add", "diff": "+new"},
    ]
    assert result.status is ToolStatus.OK and "completed" in result.content
    assert result.duration_ms == 250
    _, declined = _call_and_result(
        _read({"type": "fileChange", "changes": changes, "status": "declined"})
    )
    assert declined.status is ToolStatus.DENIED
    (running,) = _read(
        {"type": "fileChange", "changes": changes, "status": "inProgress"}, done=False
    )
    assert isinstance(running, ToolCall)


def test_an_mcp_call_is_named_by_its_server_and_tool_and_returns_its_text_or_its_error() -> None:
    arguments = {"query": "weather"}
    ok = {"type": "mcpToolCall", "server": "srv", "tool": "lookup", "arguments": arguments}
    call, result = _call_and_result(
        _read(
            {
                **ok,
                "status": "completed",
                "result": {
                    "content": [{"type": "text", "text": "sunny"}, {"type": "text", "text": "warm"}]
                },
                "durationMs": 40,
            }
        )
    )
    assert call.name == "mcp:srv/lookup" and call.tool_input == arguments
    assert (result.content, result.status, result.duration_ms) == ("sunny\nwarm", ToolStatus.OK, 40)
    _, failed = _call_and_result(
        _read({**ok, "status": "failed", "error": {"message": "boom"}, "result": None})
    )
    assert (failed.content, failed.status) == ("boom", ToolStatus.ERROR)
    _, structured = _call_and_result(
        _read(
            {**ok, "status": "completed", "result": {"content": [], "structuredContent": {"n": 1}}}
        )
    )
    assert "structuredContent" in structured.content


def test_a_dynamic_tool_call_fails_when_it_says_it_did_not_succeed() -> None:
    item = {
        "type": "dynamicToolCall",
        "tool": "browser",
        "arguments": {"url": "x"},
        "status": "completed",
    }
    _, ok = _call_and_result(
        _read({**item, "success": True, "contentItems": [{"type": "inputText", "text": "page"}]})
    )
    assert (ok.content, ok.status) == ("page", ToolStatus.OK)
    _, bad = _call_and_result(_read({**item, "success": False, "contentItems": []}))
    assert bad.status is ToolStatus.ERROR
    call, _ = _call_and_result(_read({**item, "success": True, "contentItems": None}))
    assert call.name == "browser"


def test_search_view_sleep_and_image_generation_each_have_a_call_and_a_result() -> None:
    call, result = _call_and_result(
        _read(
            {
                "type": "webSearch",
                "query": "rust async",
                "action": {"type": "search"},
                "results": None,
            }
        )
    )
    assert call.name == "web_search" and call.tool_input == {"query": "rust async"}
    assert '"action"' in result.content
    call, _ = _call_and_result(_read({"type": "imageView", "path": "/work/x.png"}))
    assert (call.name, call.tool_input) == ("view_image", {"path": "/work/x.png"})
    call, _ = _call_and_result(_read({"type": "sleep", "durationMs": 5}))
    assert call.name == "sleep"
    _, made = _call_and_result(
        _read({"type": "imageGeneration", "status": "completed", "savedPath": "/work/g.png"})
    )
    assert (made.content, made.status) == ("/work/g.png", ToolStatus.OK)


def test_a_plan_is_reasoning_and_review_mode_is_a_notice_and_the_review_text() -> None:
    (plan,) = _read({"type": "plan", "text": "1. read 2. write"})
    assert (
        isinstance(plan, Reasoning) and plan.text == "1. read 2. write" and plan.key == "plan:it-1"
    )
    (entered,) = _read({"type": "enteredReviewMode", "review": "the diff"})
    assert isinstance(entered, Notice) and not entered.opens_turn and "the diff" in entered.text
    (exited,) = _read({"type": "exitedReviewMode", "review": "looks fine"})
    assert isinstance(exited, AgentText) and exited.text == "looks fine"


def test_the_items_that_are_only_the_models_bookkeeping_give_nothing_and_no_report() -> None:
    for kind in ("hookPrompt", "functionCallOutput", "subAgentActivity"):
        assert _read({"type": kind}) == []
    (unknown,) = _read({"type": "holographicLedger"})
    assert isinstance(unknown, Anomaly) and unknown.anomaly is AnomalyKind.UNKNOWN_TYPE


def test_a_wait_call_returns_the_messages_of_the_agents_it_waited_for() -> None:
    item = {
        "type": "collabAgentToolCall",
        "tool": "wait",
        "status": "completed",
        "receiverThreadIds": ["c1", "c2"],
        "agentsStates": {
            "c1": {"status": "completed", "message": "ALPHA"},
            "c2": {"status": "running", "message": None},
        },
    }
    _, result = _call_and_result(_read(item))
    assert result.content == "c1: ALPHA"
    spawn = {
        **item,
        "tool": "spawnAgent",
        "agentsStates": {"c1": {"status": "pendingInit", "message": None}},
    }
    entries = _read(spawn)
    assert entries[-1].key == "result:it-1" and isinstance(entries[-1], ToolResult)
    assert entries[-1].content == "c1, c2"


def test_a_turn_that_failed_still_writes_task_complete_and_its_error_is_the_failure() -> None:
    line = {
        "type": "event_msg",
        "timestamp": "2026-11-12T14:35:47.880Z",
        "ordinal": 41,
        "payload": {
            "type": "task_complete",
            "turn_id": TURN,
            "error": {"message": "model not supported", "codex_error_info": "model_not_found"},
        },
    }
    (failure,) = parse_rollout_line(line, record=MAIN_RECORD)
    assert isinstance(failure, Failure) and failure.message == "model not supported"
    assert failure.turn == TURN

from __future__ import annotations

import asyncio
from pathlib import Path

from base.lm.harness.codex.tests.support import replay, start
from base.lm.harness.delivery.tests.sent import send
from base.lm.harness.events import AssistantText, SubagentText, ToolResult, ToolUse, TurnCompleted


async def test_a_spawned_threads_messages_are_subagent_text_of_the_spawn_call(
    tmp_path: Path,
) -> None:
    harness = await start(tmp_path)
    try:
        turn = await send(harness, "delegate")
        await harness.turn_started(1)
        replay(harness.server, "subagent_thread", "spawn")
        replay(harness.server, "subagent_thread", "child")
        harness.server.message("DONE")
        harness.server.complete()
        outcome = await asyncio.wait_for(turn.outcome(), 2)
        texts = harness.of(SubagentText)
        assert [(e.tool_use_id, e.text, e.turn_id) for e in texts] == [
            ("call-spawn", "ALPHA.", turn.turn_id),
            ("call-spawn", "BETA", turn.turn_id),
        ]
        assert outcome.text == "DONE", "the child's final answer is not the parent's reply"
        assert [e.text for e in harness.of(AssistantText)] == ["DONE"]
        assert len(harness.of(TurnCompleted)) == 1, "the child's turn does not end ours"
    finally:
        await harness.stop()


async def test_a_spawned_threads_tool_items_are_attributed_to_the_spawn_call(
    tmp_path: Path,
) -> None:
    harness = await start(tmp_path)
    try:
        turn = await send(harness, "delegate")
        await harness.turn_started(1)
        replay(harness.server, "subagent_thread", "spawn")
        replay(harness.server, "subagent_thread", "child")
        harness.server.message("DONE")
        harness.server.complete()
        await asyncio.wait_for(turn.outcome(), 2)
        calls = [e for e in harness.of(ToolUse) if e.tool_use_id == "c1"]
        results = [e for e in harness.of(ToolResult) if e.tool_use_id == "c1"]
        assert [(e.name, e.parent_tool_use_id, e.turn_id) for e in calls] == [
            ("command", "call-spawn", turn.turn_id)
        ], "a child item that was only ever completed still shows its call"
        assert [(e.is_error, e.parent_tool_use_id) for e in results] == [(False, "call-spawn")]
        mains = [e for e in harness.of(ToolUse) if e.tool_use_id == "call-spawn"]
        assert [e.parent_tool_use_id for e in mains] == [None], "the spawn call is the main agent's"
    finally:
        await harness.stop()


async def test_a_started_child_item_is_not_shown_twice(tmp_path: Path) -> None:
    harness = await start(tmp_path)
    try:
        turn = await send(harness, "delegate")
        await harness.turn_started(1)
        replay(harness.server, "subagent_thread", "spawn")
        for method, status in (("item/started", "inProgress"), ("item/completed", "completed")):
            harness.server.emit(
                method,
                {
                    "threadId": "child-thread-1",
                    "turnId": "child-turn-1",
                    "item": {
                        "type": "commandExecution",
                        "id": "c2",
                        "command": "ls",
                        "status": status,
                    },
                },
            )
        harness.server.message("DONE")
        harness.server.complete()
        await asyncio.wait_for(turn.outcome(), 2)
        assert [e.parent_tool_use_id for e in harness.of(ToolUse) if e.tool_use_id == "c2"] == [
            "call-spawn"
        ]
        assert [e.parent_tool_use_id for e in harness.of(ToolResult) if e.tool_use_id == "c2"] == [
            "call-spawn"
        ]
    finally:
        await harness.stop()


async def test_a_thread_no_spawn_call_named_is_not_shown(tmp_path: Path) -> None:
    harness = await start(tmp_path)
    try:
        turn = await send(harness, "delegate")
        await harness.turn_started(1)
        replay(harness.server, "subagent_thread", "stranger")
        harness.server.message("DONE")
        harness.server.complete()
        await asyncio.wait_for(turn.outcome(), 2)
        assert harness.of(SubagentText) == []
    finally:
        await harness.stop()

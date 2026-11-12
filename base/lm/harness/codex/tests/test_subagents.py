from __future__ import annotations

import asyncio
from pathlib import Path

from base.clock import Clock
from base.clock.tests.fakes import clock_over
from base.clock.tests.manual_timers import ManualTimers
from base.lm.harness.codex import CHILD_SILENCE_SECONDS
from base.lm.harness.codex.tests.support import Harness, replay, start
from base.lm.harness.delivery.tests.sent import send
from base.lm.harness.events import AssistantText, SubagentText, ToolResult, ToolUse, TurnCompleted
from base.lm.harness.session import BackgroundKind


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


async def _spawn_then_finish_parent(tmp_path: Path, *, clock: Clock | None = None) -> Harness:
    harness = await start(tmp_path, clock=clock)
    turn = await send(harness, "delegate")
    await harness.turn_started(1)
    replay(harness.server, "subagent_thread", "spawn")
    harness.server.message("DONE")
    harness.server.complete()
    await asyncio.wait_for(turn.outcome(), 2)
    return harness


def _child(harness: Harness, method: str, **params: object) -> None:
    harness.server.emit(method, {"threadId": "child-thread-1", **params})


# meiva:note
# A child thread that is still working holds the session after the parent's turn ended: the host's
# idle TTL must not close the app-server it lives in.
# meiva:link base/lm/harness/codex/docs/codex.md
async def test_a_working_child_holds_the_session_until_it_goes_idle(tmp_path: Path) -> None:
    harness = await _spawn_then_finish_parent(tmp_path)
    try:
        [held] = harness.session.background_work().items
        assert held.kind is BackgroundKind.AGENT
        assert held.description.startswith("Say the word ALPHA")
        _child(harness, "turn/started", turn={"id": "child-turn-1", "status": "inProgress"})
        assert harness.session.background_work(), "a turn of the child keeps it held"
        _child(harness, "turn/completed", turn={"id": "child-turn-1", "status": "completed"})
        assert not harness.session.background_work(), "an idle child is not work"
        _child(harness, "thread/status/changed", status={"type": "active", "activeFlags": []})
        assert harness.session.background_work(), "a child given more work holds it again"
        _child(harness, "thread/status/changed", status={"type": "idle"})
        assert not harness.session.background_work()
    finally:
        await harness.stop()


async def test_a_child_that_failed_or_was_closed_does_not_hold_the_session(
    tmp_path: Path,
) -> None:
    harness = await _spawn_then_finish_parent(tmp_path)
    try:
        _child(harness, "error", turnId="child-turn-1", willRetry=True, error={"message": "x"})
        assert harness.session.background_work(), "an error that will be retried is not the end"
        _child(harness, "error", turnId="child-turn-1", willRetry=False, error={"message": "x"})
        assert not harness.session.background_work()
        _child(harness, "turn/started", turn={"id": "child-turn-2", "status": "inProgress"})
        assert harness.session.background_work()
        harness.server.emit(
            "item/completed",
            {
                "threadId": harness.server.thread_id,
                "turnId": "t-after",
                "item": {
                    "type": "collabAgentToolCall",
                    "id": "call-close",
                    "tool": "closeAgent",
                    "status": "completed",
                    "receiverThreadIds": ["child-thread-1"],
                },
            },
        )
        assert not harness.session.background_work()
    finally:
        await harness.stop()


async def test_a_child_the_spawn_call_reports_finished_holds_nothing(tmp_path: Path) -> None:
    harness = await start(tmp_path)
    try:
        turn = await send(harness, "delegate")
        await harness.turn_started(1)
        harness.server.emit(
            "item/completed",
            {
                "threadId": harness.server.thread_id,
                "turnId": harness.server.current_turn,
                "item": {
                    "type": "collabAgentToolCall",
                    "id": "call-spawn",
                    "tool": "spawnAgent",
                    "status": "completed",
                    "receiverThreadIds": ["child-thread-1"],
                    "prompt": "quick",
                    "agentsStates": {"child-thread-1": {"status": "completed", "message": "ok"}},
                },
            },
        )
        harness.server.complete()
        await asyncio.wait_for(turn.outcome(), 2)
        assert not harness.session.background_work()
    finally:
        await harness.stop()


async def test_a_child_that_goes_silent_stops_holding_the_session_after_the_bound(
    tmp_path: Path,
) -> None:
    timers = ManualTimers()
    harness = await _spawn_then_finish_parent(tmp_path, clock=clock_over(timers))
    try:
        timers.advance(CHILD_SILENCE_SECONDS - 1)
        assert harness.session.background_work(), "inside the bound it still holds"
        _child(
            harness,
            "item/completed",
            turnId="child-turn-1",
            item={"type": "agentMessage", "id": "m", "text": "still going", "phase": "commentary"},
        )
        timers.advance(CHILD_SILENCE_SECONDS - 1)
        assert harness.session.background_work(), "any frame of the child restarts the bound"
        timers.advance(2)
        assert not harness.session.background_work(), "no frame for the whole bound: finished"
        _child(harness, "turn/started", turn={"id": "child-turn-2", "status": "inProgress"})
        assert harness.session.background_work(), "a child that speaks again is working again"
    finally:
        await harness.stop()


async def test_a_child_that_ended_before_its_spawn_call_completed_is_not_held(
    tmp_path: Path,
) -> None:
    harness = await start(tmp_path)
    try:
        turn = await send(harness, "delegate")
        await harness.turn_started(1)
        _child(harness, "turn/completed", turn={"id": "child-turn-1", "status": "completed"})
        replay(harness.server, "subagent_thread", "spawn")
        harness.server.complete()
        await asyncio.wait_for(turn.outcome(), 2)
        assert not harness.session.background_work(), (
            "its end came first and nothing clears it later"
        )
    finally:
        await harness.stop()


async def test_a_dead_app_server_holds_nothing(tmp_path: Path) -> None:
    harness = await _spawn_then_finish_parent(tmp_path)
    try:
        assert harness.session.background_work()
        await harness.server.close()
        assert not harness.session.background_work(), "nothing is running in a dead app-server"
    finally:
        await harness.stop()


async def test_a_child_that_only_sends_completed_items_is_held_across_the_bound(
    tmp_path: Path,
) -> None:
    timers = ManualTimers()
    harness = await _spawn_then_finish_parent(tmp_path, clock=clock_over(timers))
    try:
        for step in range(3):
            timers.advance(CHILD_SILENCE_SECONDS / 2)
            _child(
                harness,
                "item/completed",
                turnId="child-turn-1",
                item={
                    "type": "commandExecution",
                    "id": f"c{step}",
                    "command": "ls",
                    "status": "completed",
                },
            )
            assert harness.session.background_work(), "each completed item is a sign of life"
        timers.advance(CHILD_SILENCE_SECONDS)
        assert not harness.session.background_work()
    finally:
        await harness.stop()

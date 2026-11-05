from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path

from agent.graph.claim.harness_activity import HarnessTurnProjector
from base.agents.history.timeline_item import TimelineItem
from base.clock import SYSTEM_TIMERS, Clock, ClockConfig
from base.clock.tests.manual_timers import ManualTimers
from base.lm.harness import (
    AssistantText,
    ReasoningDelta,
    ReasoningText,
    TextDelta,
    ToolResult,
    ToolUse,
)

START = datetime(2026, 10, 25, 1, 2, 3, tzinfo=UTC)


def _projector(clock: Clock) -> tuple[HarnessTurnProjector, dict[str, TimelineItem]]:
    items: dict[str, TimelineItem] = {}

    async def checkpoint(item: TimelineItem, msg_count: int) -> None:
        items[item.item_id] = item

    return HarnessTurnProjector(checkpoint=checkpoint, base_idx=2, clock=clock), items


def _tool(tool_use_id: str = "call") -> ToolUse:
    return ToolUse(
        turn_id="turn", tool_use_id=tool_use_id, name="Bash", tool_input={"command": "echo ok"}
    )


def _result(tool_use_id: str = "call") -> ToolResult:
    return ToolResult(turn_id="turn", tool_use_id=tool_use_id, content="ok", is_error=False)


async def test_each_projection_step_uses_the_clock_time_source() -> None:
    timers = ManualTimers(wall_start=START)
    clock = Clock(ClockConfig("UTC", "UTC", False), timers=timers, now=timers.now)
    projector, items = _projector(clock)
    await projector.on_event(ReasoningDelta(turn_id="turn", text="planning"))
    timers.advance(1)
    await projector.on_event(ReasoningText(turn_id="turn", text="planning"))
    timers.advance(2)
    await projector.on_event(_tool())
    timers.advance(4)
    await projector.on_event(_result())
    timers.advance(1)
    await projector.on_event(ReasoningText(turn_id="turn", text="replying"))
    timers.advance(2)
    await projector.on_event(AssistantText(turn_id="turn", text="done"))
    await projector.finish()

    reasoning, code, result, followup, reply = items.values()
    assert reasoning.reasoning_ms == 1000
    assert code.code_elapsed_ms == 2000
    assert code.created_at == (START + timedelta(seconds=3)).isoformat()
    assert result.exec_ms == 4000
    assert result.created_at == (START + timedelta(seconds=7)).isoformat()
    assert followup.reasoning_ms == 1000
    assert reply.created_at == timers.now().isoformat()


async def test_backward_time_still_clamps_execution_duration() -> None:
    moments = iter((START, START + timedelta(seconds=3), START + timedelta(seconds=2)))
    projector, items = _projector(
        Clock(ClockConfig("UTC", "UTC", False), timers=SYSTEM_TIMERS, now=moments.__next__)
    )
    await projector.on_event(_tool())
    await projector.on_event(_result())
    assert items["3.0"].exec_ms == 0


async def test_parallel_results_keep_their_slots_and_unknown_results_do_not_add_items() -> None:
    timers = ManualTimers(wall_start=START)
    clock = Clock(ClockConfig("UTC", "UTC", False), timers=timers, now=timers.now)
    projector, items = _projector(clock)
    await projector.on_event(_tool("first"))
    await projector.on_event(_tool("second"))
    await projector.on_event(_result("unknown"))
    await projector.on_event(AssistantText(turn_id="turn", text="Waiting on a tool."))
    timers.advance(5)
    await projector.on_event(_result("second"))
    await projector.finish()

    assert [(item.kind, item.payload) for item in items.values()] == [
        ("agent_code", "echo ok"),
        ("code_output", "Tool result unavailable"),
        ("agent_code", "echo ok"),
        ("code_output", "ok"),
        ("agent_chat", "Waiting on a tool."),
    ]
    assert items["3.0"].exec_ms is None
    assert items["5.0"].exec_ms == 5000


async def test_silent_provider_does_not_hold_the_trailing_delta() -> None:
    timers = ManualTimers(wall_start=START)
    clock = Clock(ClockConfig("UTC", "UTC", False), timers=timers, now=timers.now)
    updated = asyncio.Event()

    async def checkpoint(item: TimelineItem, msg_count: int) -> None:
        if item.payload == "partial response":
            updated.set()

    projector = HarnessTurnProjector(checkpoint=checkpoint, base_idx=2, clock=clock)
    writer = asyncio.create_task(projector.flush_text())
    try:
        await projector.on_event(TextDelta(turn_id="turn", text="partial"))
        await projector.on_event(TextDelta(turn_id="turn", text=" response"))
        await asyncio.wait_for(updated.wait(), timeout=2)
    finally:
        writer.cancel()
        with suppress(asyncio.CancelledError):
            await writer


# meiva:note
# The child runs only this checkout's fixed script and isolates its configuration mutations.
def test_existing_clock_ignores_unused_rendering_config_until_the_next_capture(
    tmp_path: Path,
) -> None:
    child_env = {key: value for key, value in os.environ.items() if not key.startswith("MEIVA_")}
    child_env.update(
        {
            "MEIVA_HOME": str(tmp_path),
            "MEIVA_CONFIG_FETCH": "skip",
            "MEIVA_TIMEZONE": "UTC",
            "MEIVA_MESSAGE_TIMESTAMP_WEEKDAY": "false",
        }
    )
    completed = subprocess.run(  # noqa: S603
        [sys.executable, str(Path(__file__).with_name("projector_clock_child.py"))],
        env=child_env,
        text=True,
        capture_output=True,
        timeout=30,
        check=True,
    )
    result = json.loads(completed.stdout.splitlines()[-1])

    assert result == {"projected": True, "next_capture_refused": True, "initial_bad_refused": True}

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from langchain_core.messages import AIMessage, ToolMessage

from agent.graph.claim.harness_turn import HarnessTurnProjector
from base.agents.messages.inbound import InterruptReason
from base.agents.observation.turn_progress import TurnProgress
from base.clock import SYSTEM_TIMERS, Clock, ClockConfig
from base.clock.configuration import clock_config_from_boot
from base.clock.tests.manual_timers import ManualTimers
from base.config import ConfigBoot
from base.events.live.tests.live_capture import LiveEvents, idle_publisher
from base.lm.harness import (
    AssistantText,
    HarnessTurn,
    ReasoningDelta,
    ReasoningText,
    ToolResult,
    ToolUse,
    Usage,
)

START = datetime(2026, 10, 25, 1, 2, 3, tzinfo=UTC)


def _turn(text: str) -> HarnessTurn:
    return HarnessTurn(
        turn_id="turn",
        text=text,
        usage=Usage(),
        cost_usd=None,
        subtype="success",
        is_error=False,
        interrupted=False,
    )


def _projector(clock: Clock) -> HarnessTurnProjector:
    return HarnessTurnProjector(
        event_publisher=idle_publisher(),
        agent_id=7,
        base_idx=2,
        turn_progress=TurnProgress(),
        clock=clock,
    )


def _tool() -> ToolUse:
    return ToolUse(
        turn_id="turn", tool_use_id="call", name="Bash", tool_input={"command": "echo ok"}
    )


def _result(tool_use_id: str = "call") -> ToolResult:
    return ToolResult(turn_id="turn", tool_use_id=tool_use_id, content="ok", is_error=False)


async def test_each_projection_step_uses_the_clock_time_source() -> None:
    timers = ManualTimers(wall_start=START)
    clock = Clock(ClockConfig("UTC", "UTC", False), timers=timers, now=timers.now)
    projector = _projector(clock)
    projector.on_event(ReasoningDelta(turn_id="turn", text="planning"))
    timers.advance(1)
    projector.on_event(ReasoningText(turn_id="turn", text="planning"))
    timers.advance(2)
    projector.on_event(_tool())
    timers.advance(4)
    projector.on_event(_result())
    timers.advance(1)
    projector.on_event(ReasoningText(turn_id="turn", text="replying"))
    timers.advance(2)
    projector.on_event(AssistantText(turn_id="turn", text="done"))
    timers.advance(1)
    messages = projector.finish((_turn("done"),))

    assert len(messages) == 3
    assert messages[0].additional_kwargs["meiva_reasoning_ms_by_block"] == {"0": 1000}
    assert messages[0].additional_kwargs["meiva_code_ms_by_block"] == {"1": 2000}
    assert (
        messages[0].additional_kwargs["meiva_created_at"]
        == (START + timedelta(seconds=3)).isoformat()
    )
    assert messages[1].additional_kwargs["meiva_exec_ms"] == 4000
    assert (
        messages[1].additional_kwargs["meiva_exec_started_at"]
        == (START + timedelta(seconds=3)).isoformat()
    )
    assert (
        messages[1].additional_kwargs["meiva_created_at"]
        == (START + timedelta(seconds=7)).isoformat()
    )
    assert messages[2].additional_kwargs["meiva_reasoning_ms_by_block"] == {"0": 1000}
    assert messages[2].additional_kwargs["meiva_created_at"] == timers.now().isoformat()


async def test_flush_and_reasoning_end_take_separate_instants() -> None:
    moments = iter(START + timedelta(seconds=n) for n in range(4))
    clock = Clock(ClockConfig("UTC", "UTC", False), timers=SYSTEM_TIMERS, now=moments.__next__)
    projector = _projector(clock)
    projector.on_event(AssistantText(turn_id="turn", text="first"))
    projector.on_event(ReasoningText(turn_id="turn", text="second"))
    messages = projector.finish((_turn("done"),))

    assert messages[0].content == "first"
    assert (
        messages[0].additional_kwargs["meiva_created_at"]
        == (START + timedelta(seconds=1)).isoformat()
    )
    assert messages[1].additional_kwargs["meiva_reasoning_ms_by_block"] == {"0": 1000}
    assert (
        messages[1].additional_kwargs["meiva_created_at"]
        == (START + timedelta(seconds=3)).isoformat()
    )


async def test_unknown_tool_result_and_unstamped_fallback_need_no_later_instant() -> None:
    moments = iter((START,))
    projector = _projector(
        Clock(ClockConfig("UTC", "UTC", False), timers=SYSTEM_TIMERS, now=moments.__next__)
    )
    projector.on_event(_result("unknown"))
    projector.on_event(AssistantText(turn_id="turn", text="answer"))
    messages = projector.finish((_turn("answer"),))

    assert len(messages) == 1
    assert messages[0].content == "answer"
    assert "meiva_created_at" not in messages[0].additional_kwargs


async def test_interrupt_keeps_tool_messages_without_a_finish_instant() -> None:
    moments = iter((START, START + timedelta(seconds=1)))
    projector = _projector(
        Clock(ClockConfig("UTC", "UTC", False), timers=SYSTEM_TIMERS, now=moments.__next__)
    )
    projector.on_event(_tool())
    items = projector.settle_interrupt(InterruptReason.SYSTEM)

    assert len(items) == 2
    assert isinstance(items[1], ToolMessage)
    assert items[1].tool_call_id == "call"


async def test_backward_time_still_clamps_execution_duration() -> None:
    moments = iter((START, START + timedelta(seconds=3), START + timedelta(seconds=2)))
    projector = _projector(
        Clock(ClockConfig("UTC", "UTC", False), timers=SYSTEM_TIMERS, now=moments.__next__)
    )
    projector.on_event(_tool())
    projector.on_event(_result())
    messages = projector.finish((_turn(""),))

    assert len(messages) == 2
    assert messages[1].additional_kwargs["meiva_exec_ms"] == 0


async def test_real_clock_projects_to_the_actual_publisher(live_events: LiveEvents) -> None:
    owner = ConfigBoot()
    owner.read_process_environment()
    clock = Clock(clock_config_from_boot(owner), timers=SYSTEM_TIMERS)
    projector = HarnessTurnProjector(
        event_publisher=live_events.publisher,
        agent_id=7,
        base_idx=2,
        turn_progress=TurnProgress(),
        clock=clock,
    )
    projector.on_event(_tool())
    projector.on_event(_result())
    messages = projector.finish((_turn("done"),))
    events = await live_events.events()

    assert [event["role"] for event in events] == [
        "code_start",
        "code_delta",
        "timeline_snapshot",
        "exec_start",
        "exec_output",
        "llm_done",
    ]
    assert events[4]["content"] == messages[1].content == "ok"
    first = messages[0]
    assert isinstance(first, AIMessage)
    snapshot = events[2]["items"]
    assert [item["created_at"] for item in snapshot] == [
        first.additional_kwargs["meiva_created_at"]
    ]
    assert snapshot[0]["payload"] == first.tool_calls[0]["args"]["code"]
    assert (
        messages[1].additional_kwargs["meiva_exec_started_at"]
        == first.additional_kwargs["meiva_created_at"]
    )
    for message in messages:
        moment = datetime.fromisoformat(message.additional_kwargs["meiva_created_at"])
        assert moment.utcoffset() == timedelta(0)


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

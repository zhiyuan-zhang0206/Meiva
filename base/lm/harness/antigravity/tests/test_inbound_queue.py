from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from base.lm.harness import ForegroundSteering
from base.lm.harness.antigravity.session import InboundQueueFullError
from base.lm.harness.antigravity.tests.support import say, start
from base.lm.harness.events import TurnCompleted


async def test_a_second_message_waits_for_the_first_turn_to_finish(tmp_path: Path) -> None:
    slow = say("first")
    slow["pace"] = 0.2
    harness = await start(tmp_path, turns=[slow, say("second")])
    try:
        harness.session.submit("one")
        harness.session.submit("two")
        await harness.wait_until(lambda: len(harness.messages()) == 1)
        assert len(harness.messages()) == 1
        await harness.wait_for(lambda event: isinstance(event, TurnCompleted))
        await harness.wait_until(lambda: len(harness.messages()) == 2)
        assert harness.messages() == ["one", "two"]
    finally:
        await harness.stop()


async def test_a_full_queue_is_refused_not_buffered(tmp_path: Path) -> None:
    slow = say("first")
    slow["pace"] = 0.3
    harness = await start(tmp_path, turns=[slow, say("second")], max_pending_turns=1)
    try:
        harness.session.submit("one")
        harness.session.submit("two")
        with pytest.raises(InboundQueueFullError):
            harness.session.submit("three")
        assert harness.session.pending_turns != ()
    finally:
        await harness.stop()


async def test_an_empty_message_is_rejected(tmp_path: Path) -> None:
    harness = await start(tmp_path, turns=[say("x")])
    try:
        with pytest.raises(ValueError):
            harness.session.submit("   ")
    finally:
        await harness.stop()


async def test_steering_available_reflects_active_turn_lifecycle(tmp_path: Path) -> None:
    harness = await start(tmp_path, turns=[{"hang": True}])
    try:
        assert not harness.session.steering_available
        assert isinstance(harness.session, ForegroundSteering)
        _ = harness.session.submit("work")
        await harness.turn_started()
        assert harness.session.steering_available
        interrupt_task = asyncio.create_task(harness.session.interrupt())
        await asyncio.sleep(0.01)
        assert not harness.session.steering_available
        _ = await interrupt_task
    finally:
        await harness.stop()
        assert not harness.session.steering_available


async def test_steer_writes_to_active_session_and_preserves_running_turn(tmp_path: Path) -> None:
    steered_turn = {
        **say("first", step_index=1),
        "pace": 0.1,
        "wait_for_steer": True,
        "steered_steps": say("finish", step_index=2)["steps"],
        "result": {"response": "first\nfinish", "num_turns": 2},
    }
    harness = await start(tmp_path, turns=[steered_turn])
    try:
        first = harness.session.submit("run task", key="first-key")
        await harness.turn_started()
        assert harness.session.steering_available
        steered = await harness.session.steer("change course", key="inbound-42")
        assert steered is True
        assert harness.messages() == ["run task", "change course"]
        assert harness.session.active_turn_id == first.turn_id
        assert harness.session.pending_turns == ()
        outcome = await first.outcome()
        assert outcome.text == "first\nfinish"
    finally:
        await harness.stop()


async def test_steer_rejects_empty_text_and_empty_key(tmp_path: Path) -> None:
    harness = await start(tmp_path, turns=[{"hang": True}])
    try:
        _ = harness.session.submit("start")
        await harness.turn_started()
        with pytest.raises(ValueError, match="steering requires nonempty text and a message key"):
            _ = await harness.session.steer("", key="k1")
        with pytest.raises(ValueError, match="steering requires nonempty text and a message key"):
            _ = await harness.session.steer("   ", key="k1")
        with pytest.raises(ValueError, match="steering requires nonempty text and a message key"):
            _ = await harness.session.steer("hello", key="")
    finally:
        await harness.stop()


async def test_steer_returns_false_when_not_available(tmp_path: Path) -> None:
    harness = await start(tmp_path, turns=[say("x")])
    try:
        assert not harness.session.steering_available
        assert not await harness.session.steer("idle steer", key="inbound-1")
        assert harness.messages() == []
        await harness.session.close(reason="closed")
        assert not await harness.session.steer("closed steer", key="inbound-2")
    finally:
        await harness.stop()


async def test_steer_concurrent_same_key_raises(tmp_path: Path) -> None:
    harness = await start(tmp_path, turns=[{"hang": True}])
    try:
        _ = harness.session.submit("start")
        await harness.turn_started()
        transport = harness.session._transport
        assert transport is not None
        gate = asyncio.Event()
        real_steer = transport.steer

        async def slow_steer(text: str, **kwargs: Any) -> None:
            if text == "slow steer":
                gate.set()
                await asyncio.sleep(0.05)
            await real_steer(text, **kwargs)

        transport.steer = slow_steer

        async def run_first() -> bool:
            return await harness.session.steer("slow steer", key="concurrent-key")

        t1 = asyncio.create_task(run_first())
        await gate.wait()

        with pytest.raises(
            RuntimeError, match="steer request already pending for this message key"
        ):
            _ = await harness.session.steer("second steer", key="concurrent-key")

        assert await t1 is True
    finally:
        await harness.stop()


async def test_steer_transport_error_propagates(tmp_path: Path) -> None:
    harness = await start(tmp_path, turns=[{"hang": True}])
    try:
        _ = harness.session.submit("start")
        await harness.turn_started()
        await harness.wait_until(lambda: harness.messages() == ["start"])
        transport = harness.session._transport
        assert transport is not None
        real_steer = transport.steer
        fail_once = True

        async def flaky_steer(text: str, **kwargs: Any) -> None:
            nonlocal fail_once
            if fail_once:
                fail_once = False
                raise RuntimeError("transport broken")
            await real_steer(text, **kwargs)

        transport.steer = flaky_steer
        with pytest.raises(RuntimeError, match="transport broken"):
            _ = await harness.session.steer("steer text", key="inbound-99")
        assert await harness.session.steer("steer retry", key="inbound-99")
        assert "steer retry" in harness.messages()
    finally:
        await harness.stop()

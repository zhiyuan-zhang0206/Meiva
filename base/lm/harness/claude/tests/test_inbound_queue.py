from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from base.lm.harness import ForegroundSteering
from base.lm.harness.claude.session import InboundQueueFullError, SessionClosedError
from base.lm.harness.claude.tests.fake_sdk import Block, Delay, Say
from base.lm.harness.claude.tests.support import start


async def test_second_message_waits_for_the_result_boundary(tmp_path: Path) -> None:
    harness = await start(tmp_path, scripts=[[Say("first"), Delay(0.05)], [Say("second")]])
    try:
        first = harness.session.submit("first message")
        await asyncio.sleep(0)
        second = harness.session.submit("second message")
        await asyncio.sleep(0.01)
        assert harness.client.queries == ["first message"]
        assert harness.session.pending_turns == (second.turn_id,)
        assert (await first.outcome()).text == "first"
        assert (await second.outcome()).text == "second"
        assert harness.client.queries == ["first message", "second message"]
        assert harness.client.timeline[:3] == [
            ("query", "first message"),
            ("result", "success"),
            ("query", "second message"),
        ]
    finally:
        await harness.stop()


async def test_repeated_key_returns_the_same_ticket(tmp_path: Path) -> None:
    harness = await start(tmp_path, scripts=[[Say("only once"), Block()]])
    try:
        first = harness.session.submit("hello", key="msg-1")
        await asyncio.sleep(0)
        again = harness.session.submit("hello", key="msg-1")
        assert again is first
        assert harness.client.queries == ["hello"]
    finally:
        await harness.stop()


async def test_completed_key_replays_the_recorded_outcome(tmp_path: Path) -> None:
    harness = await start(tmp_path, scripts=[[Say("done")]])
    try:
        first = harness.session.submit("hello", key="msg-1")
        original = await first.outcome()
        replay = harness.session.submit("hello", key="msg-1")
        assert replay.deduplicated is True
        assert await replay.outcome() == original
        assert harness.client.queries == ["hello"]
    finally:
        await harness.stop()


async def test_queue_overflow_is_refused_not_buffered(tmp_path: Path) -> None:
    harness = await start(tmp_path, scripts=[[Block()]], max_pending_turns=1)
    try:
        harness.session.submit("running")
        await asyncio.sleep(0)
        harness.session.submit("queued")
        with pytest.raises(InboundQueueFullError):
            harness.session.submit("one too many")
    finally:
        await harness.stop()


async def test_close_fails_every_waiting_turn(tmp_path: Path) -> None:
    harness = await start(tmp_path, scripts=[[Block()]])
    try:
        active = harness.session.submit("running")
        await asyncio.sleep(0)
        queued = harness.session.submit("queued")
        await harness.session.close(reason="test")
        with pytest.raises(SessionClosedError):
            await active.outcome()
        with pytest.raises(SessionClosedError):
            await queued.outcome()
    finally:
        await harness.stop()


async def test_empty_message_is_refused(tmp_path: Path) -> None:
    harness = await start(tmp_path)
    try:
        with pytest.raises(ValueError):
            harness.session.submit("   ")
    finally:
        await harness.stop()


async def test_steering_available_reflects_active_turn_lifecycle(tmp_path: Path) -> None:
    harness = await start(tmp_path, scripts=[[Say("first"), Block()]])
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
    harness = await start(tmp_path, scripts=[[Say("first"), Delay(0.05), Say("finish")]])
    try:
        first = harness.session.submit("run task", key="first-key")
        await harness.turn_started()
        assert harness.session.steering_available
        steered = await harness.session.steer("change course", key="inbound-42")
        assert steered is True
        assert harness.client.queries == ["run task", "change course"]
        assert ("query", "change course") in harness.client.timeline
        assert harness.session.active_turn_id == first.turn_id
        assert harness.session.pending_turns == ()
        outcome = await first.outcome()
        assert outcome.text == "first\nfinish"
    finally:
        await harness.stop()


async def test_steer_rejects_empty_text_and_empty_key(tmp_path: Path) -> None:
    harness = await start(tmp_path, scripts=[[Block()]])
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
    harness = await start(tmp_path)
    try:
        assert not harness.session.steering_available
        assert not await harness.session.steer("idle steer", key="inbound-1")
        assert not harness.client.queries
        await harness.session.close(reason="closed")
        assert not await harness.session.steer("closed steer", key="inbound-2")
    finally:
        await harness.stop()


async def test_steer_deduplication_by_key(tmp_path: Path) -> None:
    harness = await start(tmp_path, scripts=[[Say("done"), Delay(0.05)]])
    try:
        _ = harness.session.submit("start", key="turn-key-1")
        await harness.turn_started()
        assert await harness.session.steer("same as turn key", key="turn-key-1")
        assert harness.client.queries == ["start"]
        assert await harness.session.steer("mid-turn input", key="inbound-10")
        assert harness.client.queries == ["start", "mid-turn input"]
        assert await harness.session.steer("duplicate mid-turn input", key="inbound-10")
        assert harness.client.queries == ["start", "mid-turn input"]
    finally:
        await harness.stop()


async def test_steer_concurrent_same_key_raises(tmp_path: Path) -> None:
    harness = await start(tmp_path, scripts=[[Block()]])
    try:
        _ = harness.session.submit("start")
        await harness.turn_started()
        gate = asyncio.Event()
        real_query = harness.client.query

        async def slow_query(prompt: str) -> None:
            if prompt == "slow steer":
                gate.set()
                await asyncio.sleep(0.05)
            await real_query(prompt)

        harness.client.query = slow_query

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
    harness = await start(tmp_path, scripts=[[Block()]])
    try:
        _ = harness.session.submit("start")
        await harness.turn_started()
        harness.client.query_error = RuntimeError("transport broken")
        with pytest.raises(RuntimeError, match="transport broken"):
            _ = await harness.session.steer("steer text", key="inbound-99")
        harness.client.query_error = None
        assert await harness.session.steer("steer retry", key="inbound-99")
        assert "steer retry" in harness.client.queries
    finally:
        await harness.stop()

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from base.lm.harness.claude import InboundQueueFullError, SessionClosedError
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

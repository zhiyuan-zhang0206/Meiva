from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from base.lm.harness.codex.session import InboundQueueFullError
from base.lm.harness.codex.tests.support import start, turn_text


async def test_messages_wait_for_the_turn_boundary(tmp_path: Path) -> None:
    harness = await start(tmp_path)
    try:
        first = harness.session.submit("one")
        await harness.turn_started(1)
        second = harness.session.submit("two")
        await asyncio.sleep(0.01)
        assert len(harness.server.params_for("turn/start")) == 1
        assert harness.session.pending_turns == (second.turn_id,)
        assert harness.session.active_turn_id == first.turn_id
        harness.server.message("done one")
        harness.server.complete()
        assert (await asyncio.wait_for(first.outcome(), 2)).text == "done one"
        await harness.turn_started(2)
        texts = [turn_text(params) for params in harness.server.params_for("turn/start")]
        assert texts == ["one", "two"]
        harness.server.message("done two")
        harness.server.complete()
        assert (await asyncio.wait_for(second.outcome(), 2)).text == "done two"
    finally:
        await harness.stop()


async def test_same_key_returns_the_same_ticket(tmp_path: Path) -> None:
    harness = await start(tmp_path)
    try:
        first = harness.session.submit("only once", key="dedupe-1")
        second = harness.session.submit("only once", key="dedupe-1")
        assert second is first
        await harness.turn_started()
        harness.server.message("ok")
        harness.server.complete()
        await asyncio.wait_for(first.outcome(), 2)
        assert len(harness.server.params_for("turn/start")) == 1
    finally:
        await harness.stop()


async def test_completed_key_replays_the_recorded_outcome(tmp_path: Path) -> None:
    harness = await start(tmp_path)
    try:
        first = harness.session.submit("first pass", key="dedupe-2")
        await harness.turn_started()
        harness.server.message("recorded answer")
        harness.server.complete()
        outcome = await asyncio.wait_for(first.outcome(), 2)
        replayed = harness.session.submit("first pass", key="dedupe-2")
        assert replayed.deduplicated is True
        assert replayed.turn_id == first.turn_id
        assert (await asyncio.wait_for(replayed.outcome(), 2)).text == outcome.text
        assert len(harness.server.params_for("turn/start")) == 1
    finally:
        await harness.stop()


async def test_queue_overflow_is_refused(tmp_path: Path) -> None:
    harness = await start(tmp_path, max_pending_turns=1)
    try:
        harness.session.submit("one")
        await harness.turn_started()
        harness.session.submit("two")
        with pytest.raises(InboundQueueFullError):
            harness.session.submit("three")
        assert harness.session.pending_turns == (harness.session.pending_turns[0],)
    finally:
        await harness.stop()


async def test_usage_accumulates_across_turns(tmp_path: Path) -> None:
    harness = await start(tmp_path)
    try:
        first = harness.session.submit("one")
        await harness.turn_started(1)
        harness.server.usage(input_tokens=10, output_tokens=2, cached_input_tokens=4)
        harness.server.message("a")
        harness.server.complete()
        await asyncio.wait_for(first.outcome(), 2)
        second = harness.session.submit("two")
        await harness.turn_started(2)
        harness.server.usage(input_tokens=20, output_tokens=3, cached_input_tokens=6)
        harness.server.message("b")
        harness.server.complete()
        await asyncio.wait_for(second.outcome(), 2)
        totals = harness.session.totals
        assert totals.turns == 2
        assert totals.input_tokens == 30
        assert totals.output_tokens == 5
        assert totals.cache_read_input_tokens == 10
    finally:
        await harness.stop()


async def test_droppable_deltas_are_dropped_under_backpressure(tmp_path: Path) -> None:
    harness = await start(tmp_path, max_buffered_events=1)
    try:
        harness.session.submit("one")
        await harness.turn_started()
        for index in range(5):
            harness.server.delta(str(index))
        assert harness.session.dropped_deltas > 0
    finally:
        await harness.stop()

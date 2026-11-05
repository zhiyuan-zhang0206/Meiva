from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from base.lm.harness.codex.frames import INTERNAL_ERROR, INVALID_REQUEST, JsonObject, JsonRpcError
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


async def test_steer_waits_for_matching_completed_user_item(tmp_path: Path) -> None:
    harness = await start(tmp_path)
    try:
        first = harness.session.submit("one")
        await harness.turn_started()
        assert harness.session.steering_available
        steer = asyncio.create_task(harness.session.steer("change direction", key="inbound-42"))
        await asyncio.sleep(0)
        assert harness.server.params_for("turn/steer") == [
            {
                "threadId": harness.server.thread_id,
                "expectedTurnId": harness.server.current_turn,
                "clientUserMessageId": "inbound-42",
                "input": [{"type": "text", "text": "change direction"}],
            }
        ]
        params: JsonObject = {
            "threadId": harness.server.thread_id,
            "turnId": harness.server.current_turn,
            "item": {"type": "userMessage", "id": "user-2", "clientId": "inbound-42"},
        }
        harness.server.emit("item/started", params)
        harness.server.emit("item/completed", {**params, "turnId": "different-turn"})
        harness.server.emit(
            "item/completed",
            {**params, "item": {"type": "userMessage", "id": "user-3", "clientId": "other"}},
        )
        await asyncio.sleep(0)
        assert not steer.done()
        harness.server.emit("item/completed", params)
        assert await asyncio.wait_for(steer, 2)
        assert harness.session.active_turn_id == first.turn_id
        assert harness.session.pending_turns == ()
        assert len(harness.server.params_for("turn/start")) == 1
    finally:
        await harness.stop()


async def test_steer_rejection_and_internal_error_have_distinct_outcomes(tmp_path: Path) -> None:
    harness = await start(tmp_path)
    try:
        assert not harness.session.steering_available
        assert not await harness.session.steer("idle", key="inbound-42")
        assert not harness.server.params_for("turn/steer")
        _ = harness.session.submit("one")
        await harness.turn_started()
        harness.server.steer_error = JsonRpcError(INVALID_REQUEST, "no active turn to steer")
        assert not await harness.session.steer("rejected", key="inbound-43")
        harness.server.steer_error = JsonRpcError(INTERNAL_ERROR, "internal failure")
        with pytest.raises(JsonRpcError, match="internal failure"):
            _ = await harness.session.steer("ambiguous", key="inbound-44")
    finally:
        await harness.stop()


async def test_steer_without_receipt_at_turn_completion_is_ambiguous(tmp_path: Path) -> None:
    harness = await start(tmp_path)
    try:
        _ = harness.session.submit("one")
        await harness.turn_started()
        steer = asyncio.create_task(harness.session.steer("queued", key="inbound-42"))
        await asyncio.sleep(0)
        assert harness.server.params_for("turn/steer")
        harness.server.complete()
        assert not harness.session.steering_available
        with pytest.raises(RuntimeError, match="turn ended before the steer receipt arrived"):
            _ = await asyncio.wait_for(steer, 2)
        assert harness.session.pending_turns == ()
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

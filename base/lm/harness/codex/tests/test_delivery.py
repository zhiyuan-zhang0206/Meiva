from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from base.lm.harness.codex.frames import INTERNAL_ERROR, INVALID_REQUEST, JsonRpcError
from base.lm.harness.codex.tests.support import Harness, replay, start
from base.lm.harness.delivery import HarnessInput, ImageBlock
from base.lm.harness.events import (
    AckSignal,
    AssistantText,
    HarnessCause,
    InputInContext,
    InputLost,
    InputReceived,
    InputRejected,
    InterruptConfirmed,
    ToolResult,
    TurnCompleted,
    TurnOrigin,
    TurnStarted,
)


async def seen(harness: Harness, kind: type) -> list[Any]:
    await asyncio.sleep(0.01)
    return harness.of(kind)


async def kinds(harness: Harness, *types: type) -> list[type]:
    await asyncio.sleep(0.01)
    return [type(event) for event in harness.events if isinstance(event, types)]


async def running_turn(harness: Harness) -> str:
    ticket = harness.session.submit("one", key="first")
    await harness.turn_started()
    await asyncio.wait_for(ticket.interrupt_ready.wait(), 2)
    harness.server.emit(
        "item/completed",
        {
            "threadId": harness.server.thread_id,
            "turnId": harness.server.current_turn,
            "item": {"type": "userMessage", "id": "u-0", "clientId": "first"},
        },
    )
    await asyncio.sleep(0.01)
    harness.events.clear()
    return ticket.turn_id


async def test_a_steered_input_is_received_then_enters_the_context_of_the_running_turn(
    tmp_path: Path,
) -> None:
    harness = await start(tmp_path)
    try:
        turn_id = await running_turn(harness)
        await harness.session.deliver([HarnessInput("cid-b", "also do this")])
        sent = harness.server.params_for("turn/steer")[0]
        assert sent["clientUserMessageId"] == "cid-b"
        assert [type(e) for e in (await seen(harness, InputReceived))] == [InputReceived]
        assert (await seen(harness, InputReceived))[-1].signal is AckSignal.STRONG
        assert (await seen(harness, InputInContext)) == []
        replay(harness.server, "steer_absorbed", "ack2")
        replay(harness.server, "steer_absorbed", "end")
        context = await seen(harness, InputInContext)
        assert [(e.key, e.turn_id) for e in context] == [("cid-b", turn_id)]
        assert len(await seen(harness, TurnCompleted)) == 1
        assert (await seen(harness, TurnCompleted))[0].generation == harness.session.generation
    finally:
        await harness.stop()


async def test_an_input_without_a_completion_event_is_lost_when_the_turn_is_interrupted(
    tmp_path: Path,
) -> None:
    harness = await start(tmp_path)
    try:
        _ = await running_turn(harness)
        await harness.session.deliver([HarnessInput("cid-a", "steer then interrupt")])
        await harness.session.interrupt()
        replay(harness.server, "steer_lost_on_interrupt", "end")
        assert [e.key for e in (await seen(harness, InputLost))] == ["cid-a"]
        assert (await seen(harness, InputInContext)) == []
        order = await kinds(harness, InputLost, TurnCompleted, InterruptConfirmed)
        assert order == [InputLost, TurnCompleted, InterruptConfirmed]
    finally:
        await harness.stop()


async def test_a_command_completion_of_an_ended_turn_is_not_attributed_to_the_next_turn(
    tmp_path: Path,
) -> None:
    harness = await start(tmp_path)
    try:
        first = harness.session.submit("one")
        await harness.turn_started(1)
        old = harness.server.current_turn
        replay(harness.server, "steer_lost_on_interrupt", "end")
        await asyncio.wait_for(first.outcome(), 2)
        second = harness.session.submit("two")
        await harness.turn_started(2)
        await asyncio.wait_for(second.interrupt_ready.wait(), 2)
        replay(harness.server, "steer_lost_on_interrupt", "late_command", turn=old)
        await asyncio.sleep(0.01)
        assert (await seen(harness, ToolResult)) == []
    finally:
        await harness.stop()


async def test_a_refused_steer_is_a_rejection_event_and_refuses_the_rest_of_the_batch(
    tmp_path: Path,
) -> None:
    harness = await start(tmp_path)
    try:
        _ = await running_turn(harness)
        harness.server.steer_error = JsonRpcError(INVALID_REQUEST, "no active turn to steer")
        await harness.session.deliver([HarnessInput("k1", "a"), HarnessInput("k2", "b")])
        rejected = await seen(harness, InputRejected)
        assert [e.key for e in rejected] == ["k1", "k2"]
        assert "no active turn to steer" in rejected[0].reason
        assert (await seen(harness, InputReceived)) == []
        assert len(harness.server.params_for("turn/steer")) == 1
    finally:
        await harness.stop()


async def test_a_server_failure_is_an_error_not_a_rejection(tmp_path: Path) -> None:
    harness = await start(tmp_path)
    try:
        _ = await running_turn(harness)
        harness.server.steer_error = JsonRpcError(INTERNAL_ERROR, "boom")
        with pytest.raises(JsonRpcError):
            await harness.session.deliver([HarnessInput("k1", "a")])
        assert (await seen(harness, InputRejected)) == []
    finally:
        await harness.stop()


async def test_delivery_to_an_idle_session_starts_a_turn_carrying_the_input_key(
    tmp_path: Path,
) -> None:
    harness = await start(tmp_path)
    try:
        await harness.session.deliver([HarnessInput("k-idle", "hello")])
        start_params = harness.server.params_for("turn/start")[0]
        assert start_params["clientUserMessageId"] == "k-idle"
        started = (await seen(harness, TurnStarted))[0]
        assert (started.origin, started.keys) == (TurnOrigin.INPUT, ("k-idle",))
        assert (await seen(harness, InputReceived))[0].signal is AckSignal.STRONG
        replay(harness.server, "steer_absorbed", "ack2")
        assert (await seen(harness, InputInContext)) == []
        harness.server.emit(
            "item/completed",
            {
                "threadId": harness.server.thread_id,
                "turnId": harness.server.current_turn,
                "item": {"type": "userMessage", "id": "u", "clientId": "k-idle"},
            },
        )
        assert [e.key for e in (await seen(harness, InputInContext))] == ["k-idle"]
        assert (await seen(harness, InputInContext))[0].turn_id == started.turn_id
    finally:
        await harness.stop()


async def test_a_batch_is_delivered_in_order_and_images_ride_with_it(tmp_path: Path) -> None:
    harness = await start(tmp_path)
    try:
        _ = await running_turn(harness)
        image = ImageBlock("image/png", "aGk=")
        await harness.session.deliver(
            [HarnessInput("k1", "first"), HarnessInput("k2", "", (image,))]
        )
        sent = harness.server.params_for("turn/steer")
        assert [p["clientUserMessageId"] for p in sent] == ["k1", "k2"]
        assert sent[1]["input"] == [{"type": "image", "url": "data:image/png;base64,aGk="}]
        assert [e.key for e in (await seen(harness, InputReceived))] == ["k1", "k2"]
    finally:
        await harness.stop()


async def test_a_turn_the_server_began_is_observed_attributed_and_completed_once(
    tmp_path: Path,
) -> None:
    harness = await start(tmp_path)
    try:
        replay(harness.server, "server_started_turn", "start", turn="srv-turn")
        started = (await seen(harness, TurnStarted))[0]
        assert started.origin is TurnOrigin.HARNESS
        assert started.cause is HarnessCause.UNKNOWN
        waiting = harness.session.submit("ours")
        await asyncio.sleep(0.01)
        assert not harness.server.params_for("turn/start"), "our turn waits for the server's"
        replay(harness.server, "server_started_turn", "activity", turn="srv-turn")
        replay(harness.server, "server_started_turn", "end", turn="srv-turn")
        replay(harness.server, "server_started_turn", "end", turn="srv-turn")
        assert [e.turn_id for e in (await seen(harness, AssistantText))] == [started.turn_id]
        completions = await seen(harness, TurnCompleted)
        assert [c.turn_id for c in completions] == [started.turn_id]
        await harness.turn_started()
        assert waiting.turn_id != started.turn_id
    finally:
        await harness.stop()

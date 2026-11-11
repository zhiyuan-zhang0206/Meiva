from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from base.lm.harness.claude.delivery import key_uuid
from base.lm.harness.claude.tests.support import Harness, replay, start
from base.lm.harness.delivery import HarnessInput, ImageBlock
from base.lm.harness.events import (
    AckSignal,
    AssistantText,
    HarnessCause,
    InputInContext,
    InputReceived,
    InterruptConfirmed,
    SessionFailed,
    TurnCompleted,
    TurnOrigin,
    TurnStarted,
)


async def seen(harness: Harness, kind: type) -> list[Any]:
    await asyncio.sleep(0.01)
    return harness.of(kind)


async def first_turn(harness: Harness) -> Any:
    ticket = harness.session.submit("one", key="first")
    await asyncio.sleep(0.01)
    return ticket


async def test_an_input_written_during_a_tool_is_absorbed_into_the_running_turn(
    tmp_path: Path,
) -> None:
    harness = await start(tmp_path, passive=True)
    try:
        ticket = await first_turn(harness)
        await harness.session.deliver([HarnessInput("steer", "also this")])
        assert (await seen(harness, InputReceived))[-1].signal is AckSignal.WEAK
        assert await seen(harness, InputInContext) == []
        replay(harness.client, "absorbed_at_tool", "turn", first="first", steer="steer")
        outcome = await asyncio.wait_for(ticket.outcome(), 2)
        context = await seen(harness, InputInContext)
        assert [(e.key, e.turn_id) for e in context] == [
            ("first", ticket.turn_id),
            ("steer", ticket.turn_id),
        ]
        assert [type(e) for e in await seen(harness, TurnStarted)] == [TurnStarted]
        assert len(await seen(harness, TurnCompleted)) == 1
        assert outcome.text == "DONE ZEBRA-7731"
    finally:
        await harness.stop()


async def test_a_deferred_input_becomes_its_own_visible_turn_after_the_result(
    tmp_path: Path,
) -> None:
    harness = await start(tmp_path, passive=True)
    try:
        ticket = await first_turn(harness)
        await harness.session.deliver([HarnessInput("steer", "late input")])
        replay(harness.client, "deferred_new_turn", "turn1", first="first", steer="steer")
        outcome = await asyncio.wait_for(ticket.outcome(), 2)
        assert outcome.text == "1. Lions ... 120. Owls"
        assert [e.key for e in await seen(harness, InputInContext)] == ["first"]
        replay(harness.client, "deferred_new_turn", "turn2", first="first", steer="steer")
        await asyncio.sleep(0.01)
        started = (await seen(harness, TurnStarted))[1]
        assert started.origin is TurnOrigin.HARNESS
        assert started.cause is HarnessCause.QUEUED_INPUT_REVIVED
        assert started.keys == ("steer",)
        context = (await seen(harness, InputInContext))[-1]
        assert (context.key, context.turn_id) == ("steer", started.turn_id)
        completions = await seen(harness, TurnCompleted)
        assert [c.turn_id for c in completions] == [ticket.turn_id, started.turn_id]
        assert completions[1].text == "Noted: ZEBRA-7731"
        texts = [(e.turn_id, e.text) for e in await seen(harness, AssistantText)]
        assert texts[-1] == (started.turn_id, "Noted: ZEBRA-7731")
    finally:
        await harness.stop()


async def test_a_turn_submitted_right_after_does_not_take_the_deferred_turns_result(
    tmp_path: Path,
) -> None:
    harness = await start(tmp_path, passive=True)
    try:
        first = await first_turn(harness)
        await harness.session.deliver([HarnessInput("steer", "late input")])
        replay(harness.client, "deferred_new_turn", "turn1", first="first", steer="steer")
        await asyncio.wait_for(first.outcome(), 2)
        second = harness.session.submit("next instruction", key="next")
        await asyncio.sleep(0.01)
        replay(harness.client, "deferred_new_turn", "turn2", first="first", steer="steer")
        replay(harness.client, "deferred_new_turn", "next", next="next")
        outcome = await asyncio.wait_for(second.outcome(), 2)
        assert outcome.text == "answer to next"
        completed = {c.turn_id: c.text for c in await seen(harness, TurnCompleted)}
        assert completed[second.turn_id] == "answer to next"
        assert "Noted: ZEBRA-7731" in completed.values()
    finally:
        await harness.stop()


async def test_an_interrupted_turn_ends_and_the_queued_input_revives_as_a_new_turn(
    tmp_path: Path,
) -> None:
    harness = await start(tmp_path, passive=True)
    try:
        ticket = await first_turn(harness)
        await harness.session.deliver([HarnessInput("steer", "queued before interrupt")])
        replay(harness.client, "interrupt_revive", "interrupted", first="first", steer="steer")
        outcome = await asyncio.wait_for(ticket.outcome(), 2)
        assert outcome.interrupted
        assert len(await seen(harness, InterruptConfirmed)) == 1
        assert [e.key for e in await seen(harness, InputInContext)] == ["first"]
        replay(harness.client, "interrupt_revive", "revived", first="first", steer="steer")
        await asyncio.sleep(0.01)
        assert [e.key for e in await seen(harness, InputInContext)] == ["first", "steer"]
        assert len(await seen(harness, TurnStarted)) == 2, "the marker echo opened no turn"
        assert len(await seen(harness, TurnCompleted)) == 2
    finally:
        await harness.stop()


async def test_a_turn_the_cli_began_for_a_finished_background_task_is_observed(
    tmp_path: Path,
) -> None:
    harness = await start(tmp_path, passive=True)
    try:
        replay(harness.client, "background_wake", "task_answer")
        await asyncio.sleep(0.02)
        [started] = await seen(harness, TurnStarted)
        assert (started.origin, started.cause) == (TurnOrigin.HARNESS, HarnessCause.BACKGROUND_TASK)
        [done] = await seen(harness, TurnCompleted)
        assert done.turn_id == started.turn_id
        assert done.text == "The background command finished."
    finally:
        await harness.stop()


async def test_a_turn_the_cli_began_on_a_timer_names_the_scheduled_cause(tmp_path: Path) -> None:
    harness = await start(tmp_path, passive=True)
    try:
        replay(harness.client, "background_wake", "timer_wake")
        await asyncio.sleep(0.02)
        [started] = await seen(harness, TurnStarted)
        assert started.cause is HarnessCause.SCHEDULED_WAKE
        assert started.detail == "cron"
        assert len(await seen(harness, TurnCompleted)) == 1
    finally:
        await harness.stop()


async def test_a_write_alone_is_only_a_weak_receipt(tmp_path: Path) -> None:
    harness = await start(tmp_path, passive=True)
    try:
        await harness.session.deliver([HarnessInput("k1", "a"), HarnessInput("k2", "b")])
        received = await seen(harness, InputReceived)
        assert [(e.key, e.signal) for e in received] == [
            ("k1", AckSignal.WEAK),
            ("k2", AckSignal.WEAK),
        ]
        assert await seen(harness, InputInContext) == []
        sent = [message["uuid"] for message in harness.client.sent]
        assert sent == [key_uuid("k1"), key_uuid("k2")]
    finally:
        await harness.stop()


async def test_images_ride_as_base64_content_blocks(tmp_path: Path) -> None:
    harness = await start(tmp_path, passive=True)
    try:
        image = ImageBlock("image/png", "aGk=")
        await harness.session.deliver([HarnessInput("k1", "look", (image,))])
        content = harness.client.sent[0]["message"]["content"]
        assert content == [
            {"type": "text", "text": "look"},
            {
                "type": "image",
                "source": {"type": "base64", "media_type": "image/png", "data": "aGk="},
            },
        ]
    finally:
        await harness.stop()


async def test_a_cli_that_does_not_echo_fails_the_session_instead_of_guessing(
    tmp_path: Path,
) -> None:
    harness = await start(tmp_path, passive=True)
    try:
        ticket = await first_turn(harness)
        replay(harness.client, "deferred_new_turn", "turn1", first="nobody")
        with pytest.raises(Exception, match="replay-user-messages"):
            _ = await asyncio.wait_for(ticket.outcome(), 2)
        assert "replay-user-messages" in (await seen(harness, SessionFailed))[0].error
    finally:
        await harness.stop()


# meiva:note
# Found against the real CLI: a SessionStart hook in the user's settings reports before the CLI echoes the
# first input, and read as turn content it opened a harness turn that nothing had begun, so the input's own
# turn joined it and was reported as the CLI's doing.
# meiva:link base/lm/harness/claude/docs/claude.md
def test_a_hook_report_is_about_the_session_not_content_of_a_turn() -> None:
    from base.lm.harness.claude.inbound import MessageKind, message_kind

    assert message_kind("HookEventMessage") is MessageKind.SESSION

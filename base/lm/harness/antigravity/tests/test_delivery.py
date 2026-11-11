from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from base.lm.harness.antigravity.tests.support import Harness, running, say, start
from base.lm.harness.antigravity.trajectory import (
    AgyInterfaceError,
    PhantomAction,
    RunStatus,
    parse_trajectory,
    phantom_action,
)
from base.lm.harness.delivery import HarnessInput, ImageBlock
from base.lm.harness.delivery.tests.sent import send
from base.lm.harness.events import (
    AckSignal,
    AssistantText,
    HarnessCause,
    InputInContext,
    InputReceived,
    InputRejected,
    TurnCompleted,
    TurnOrigin,
    TurnStarted,
)

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "phantom_turn.json").read_text())


async def seen(harness: Harness, kind: type, *, wait: float = 0.05) -> list[Any]:
    await asyncio.sleep(wait)
    return harness.of(kind)


def test_the_recorded_poll_sequence_opens_and_closes_the_phantom_turn() -> None:
    actions: list[PhantomAction] = []
    phantom_open = False
    for poll in FIXTURE["poll_after_late_input"][1:]:
        action = phantom_action(
            RunStatus.RUNNING if "RUNNING" in poll["status"] else RunStatus.IDLE,
            ticket_active=False,
            phantom_open=phantom_open,
        )
        phantom_open = {
            PhantomAction.OPEN: True,
            PhantomAction.CLOSE: False,
            PhantomAction.NONE: phantom_open,
        }[action]
        actions.append(action)
    assert actions == [PhantomAction.OPEN, PhantomAction.NONE, PhantomAction.CLOSE]


def test_a_turn_of_ours_is_never_mistaken_for_a_phantom() -> None:
    action = phantom_action(RunStatus.RUNNING, ticket_active=True, phantom_open=False)
    assert action is PhantomAction.NONE


def test_the_recorded_trajectory_reply_is_decoded_and_a_wrong_shape_is_refused() -> None:
    parsed = parse_trajectory(FIXTURE["trajectory_reply"])
    assert (parsed.status, parsed.num_steps) == (RunStatus.RUNNING, 3)
    assert parsed.client_message_ids() == ("cmid-a",)
    with pytest.raises(AgyInterfaceError, match="status"):
        _ = parse_trajectory({"status": "CASCADE_RUN_STATUS_SOMETHING", "numTotalSteps": 1})
    with pytest.raises(AgyInterfaceError, match="steps"):
        _ = parse_trajectory({"status": "CASCADE_RUN_STATUS_IDLE", "numTotalSteps": 1})


def test_a_conversation_with_no_step_yet_is_idle_and_empty_not_a_wrong_shape() -> None:
    parsed = parse_trajectory(FIXTURE["trajectory_reply_empty"])
    assert (parsed.status, parsed.num_steps, parsed.steps) == (RunStatus.IDLE, 0, ())
    with pytest.raises(AgyInterfaceError, match="steps"):
        _ = parse_trajectory(
            {"status": "CASCADE_RUN_STATUS_IDLE", "numTotalSteps": 2, "trajectory": {}}
        )


async def test_an_input_for_a_running_turn_carries_its_id_and_enters_the_context(
    tmp_path: Path,
) -> None:
    harness = await start(tmp_path, turns=[{"hang": True}])
    try:
        ticket = await send(harness, "work", key="first")
        await running(harness, ["work"])
        await harness.session.deliver([HarnessInput("steer-1", "also this")])
        [sent] = harness.rpc_payloads()
        assert sent["clientMessageId"] == "steer-1"
        assert sent["items"] == [{"text": "also this"}]
        assert [(e.key, e.signal) for e in await seen(harness, InputReceived)][-1] == (
            "steer-1",
            AckSignal.WEAK,
        )
        await harness.wait_for(lambda e: isinstance(e, InputInContext) and e.key == "steer-1")
        context = [e for e in harness.of(InputInContext) if e.key == "steer-1"]
        assert context[0].turn_id == ticket.turn_id
    finally:
        await harness.stop()


async def test_images_ride_in_the_media_field(tmp_path: Path) -> None:
    harness = await start(tmp_path, turns=[{"hang": True}])
    try:
        _ = await send(harness, "work")
        await running(harness, ["work"])
        image = ImageBlock("image/png", "aGk=")
        await harness.session.deliver([HarnessInput("img-1", "look", (image,))])
        [sent] = harness.rpc_payloads()
        assert sent["media"] == [{"mimeType": "image/png", "inlineData": "aGk="}]
    finally:
        await harness.stop()


async def test_an_input_for_an_idle_conversation_starts_a_turn_on_stdin_not_a_phantom(
    tmp_path: Path,
) -> None:
    harness = await start(tmp_path, turns=[say("one"), say("two")])
    try:
        first = await send(harness, "one", key="first")
        _ = await first.outcome()
        await harness.session.deliver([HarnessInput("late", "next input")])
        await harness.wait_until(lambda: harness.messages() == ["one", "next input"])
        assert harness.rpc_payloads() == [], "no SendUserCascadeMessage for an idle conversation"
        started = (await seen(harness, TurnStarted))[-1]
        assert (started.origin, started.keys) == (TurnOrigin.INPUT, ("late",))
    finally:
        await harness.stop()


async def test_an_input_that_finds_our_turn_just_finished_is_refused_not_sent_into_a_phantom(
    tmp_path: Path,
) -> None:
    harness = await start(tmp_path, turns=[{**say("one"), "idle_before_result": 0.4}, say("two")])
    try:
        first = await send(harness, "one", key="first")
        await harness.wait_until(lambda: harness.messages() == ["one"])
        await asyncio.sleep(0.2)
        await harness.session.deliver([HarnessInput("late", "next input")])
        assert harness.rpc_payloads() == []
        assert [e.key for e in await seen(harness, InputRejected)] == ["late"]
        _ = await first.outcome()
        assert harness.messages() == ["one"]
    finally:
        await harness.stop()


async def test_a_phantom_turn_is_observed_and_its_answer_does_not_leak_into_the_next_turn(
    tmp_path: Path,
) -> None:
    harness = await start(
        tmp_path,
        turns=[say("one"), say("two")],
        extra_environ={"FAKE_AGY_STALE_CALLS": "1", "FAKE_AGY_PHANTOM_SECONDS": "1.3"},
    )
    try:
        first = await send(harness, "one", key="first")
        _ = await first.outcome()
        await harness.session.deliver([HarnessInput("late", "late input")])
        assert [p["clientMessageId"] for p in harness.rpc_payloads()] == ["late"]
        await harness.wait_for(
            lambda e: isinstance(e, TurnCompleted) and e.text.startswith("phantom")
        )
        [phantom] = [e for e in harness.of(TurnStarted) if e.origin is TurnOrigin.HARNESS]
        assert phantom.cause is HarnessCause.QUEUED_INPUT_REVIVED
        assert phantom.keys == ("late",)
        context = [e for e in harness.of(InputInContext) if e.key == "late"]
        assert context[0].turn_id == phantom.turn_id
        assert [e.text for e in harness.of(AssistantText) if e.turn_id == phantom.turn_id] == [
            "phantom answer to late input"
        ]
        second = await send(harness, "two", key="second")
        outcome = await second.outcome()
        assert outcome.text == "two", "the phantom turn's replayed steps are not this turn's"
        completed = await seen(harness, TurnCompleted)
        assert len({e.turn_id for e in completed}) == len(completed) == 3
    finally:
        await harness.stop()


async def test_a_trajectory_of_an_unexpected_shape_fails_the_delivery(tmp_path: Path) -> None:
    harness = await start(
        tmp_path,
        turns=[{"hang": True}],
        extra_environ={"FAKE_AGY_BAD_TRAJECTORY": "1"},
    )
    try:
        _ = await send(harness, "work")
        await running(harness, ["work"])
        with pytest.raises(AgyInterfaceError):
            await harness.session.deliver([HarnessInput("k", "x")])
    finally:
        await harness.stop()


def _watchers() -> list[asyncio.Task[Any]]:
    return [task for task in asyncio.all_tasks() if "_watch" in repr(task.get_coro())]


async def test_closing_with_a_phantom_turn_open_stops_the_trajectory_poll(tmp_path: Path) -> None:
    harness = await start(
        tmp_path,
        turns=[say("one")],
        extra_environ={"FAKE_AGY_STALE_CALLS": "1", "FAKE_AGY_PHANTOM_SECONDS": "30"},
    )
    first = await send(harness, "one", key="first")
    _ = await first.outcome()
    await harness.session.deliver([HarnessInput("late", "late input")])
    await harness.wait_for(lambda e: isinstance(e, TurnStarted) and e.origin is TurnOrigin.HARNESS)
    assert _watchers(), "the poll runs while the phantom turn is open"
    await asyncio.wait_for(harness.stop(), 10)
    assert _watchers() == []


async def test_closing_with_an_unsettled_input_stops_the_trajectory_poll(tmp_path: Path) -> None:
    harness = await start(
        tmp_path, turns=[{"hang": True}], extra_environ={"FAKE_AGY_BAD_TRAJECTORY": "1"}
    )
    _ = await send(harness, "work")
    await harness.wait_until(lambda: harness.messages() == ["work"])
    assert _watchers()
    await asyncio.wait_for(harness.stop(), 10)
    assert _watchers() == []


async def test_an_image_for_an_idle_conversation_starts_the_turn_with_text_and_is_steered_in_after(
    tmp_path: Path,
) -> None:
    harness = await start(tmp_path, turns=[{"hang": True}])
    try:
        image = ImageBlock("image/png", "aGk=")
        await harness.session.deliver([HarnessInput("img-1", "look at this", (image,))])
        await harness.wait_until(lambda: len(harness.rpc_payloads()) == 1)
        [sent] = harness.rpc_payloads()
        assert harness.messages()[0] == "look at this\n\n(The image for this message follows next.)"
        assert sent["clientMessageId"] == "img-1:images"
        assert sent["media"] == [{"mimeType": "image/png", "inlineData": "aGk="}]
        started = (await seen(harness, TurnStarted))[-1]
        assert started.keys == ("img-1",), "the caller's key is the text input that began the turn"
    finally:
        await harness.stop()

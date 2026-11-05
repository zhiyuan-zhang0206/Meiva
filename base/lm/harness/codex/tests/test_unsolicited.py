from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from base.lm.harness.base_session import TurnTicket
from base.lm.harness.codex.frames import INVALID_REQUEST, JsonRpcError
from base.lm.harness.codex.tests.support import start
from base.lm.harness.events import (
    AssistantText,
    ForegroundAccepted,
    SessionFailed,
    TextDelta,
    ToolUse,
    TurnCompleted,
    TurnFaulted,
    UnsolicitedOutput,
)
from base.lm.harness.session import SessionClosedError


# meiva:note
# Wait until `turn/start` has answered and the ticket knows the provider's name. The
# fake appends the turn id inside the request handler; the adapter binds it when the
# response is read, one event-loop turn later. Every test below that asserts "a
# foreign name is not adopted" has to start from a bound ticket, or it would pass
# because nothing was bound yet rather than because the adoption is gone.
async def bound(turn: TurnTicket) -> None:
    for _ in range(1000):
        if turn.provider_turn_id is not None:
            return
        await asyncio.sleep(0.002)
    raise AssertionError("the ticket never learned the provider's turn name")


# meiva:note
# The app-server runs turns this session never started: a queued message, a review,
# a compaction, or — across a resume — a turn an earlier session ran. Its terminal
# message names a turn no ticket of ours holds, and the adapter used to report that
# as a session fault, which ended whichever turn came next (#5663). Every test here
# feeds a message the session did not ask for and asserts the same three things:
# the wait it is not ours does not end, no failure is invented, and the reply that
# arrives later is the turn's own.
# meiva:link base/lm/harness/docs/attribution.meiva.okf.md
def completed(turn_id: str, *, text: str = "BGDONE") -> dict[str, object]:
    return {
        "id": turn_id,
        "status": "completed",
        "durationMs": 7,
        "items": [{"type": "agentMessage", "id": "msg-x", "text": text, "phase": "final_answer"}],
    }


async def test_a_completion_for_a_turn_we_did_not_start_does_not_poison_the_next_one(
    tmp_path: Path,
) -> None:
    delivered: list[UnsolicitedOutput] = []
    harness = await start(tmp_path, unsolicited=delivered.append)
    try:
        harness.server.emit(
            "turn/completed",
            {"threadId": harness.server.thread_id, "turn": completed("codex-turn-elsewhere")},
        )
        await asyncio.sleep(0)

        assert harness.of(TurnFaulted) == []
        assert harness.of(SessionFailed) == []
        assert [event.text for event in delivered] == ["BGDONE"]

        turn = harness.session.submit("hello")
        await harness.turn_started(1)
        harness.server.message("reply")
        harness.server.complete()

        assert (await asyncio.wait_for(turn.outcome(), 2)).text == "reply"
    finally:
        await harness.stop()


async def test_a_completion_for_another_turn_does_not_truncate_a_live_turn(
    tmp_path: Path,
) -> None:
    harness = await start(tmp_path)
    try:
        turn = harness.session.submit("slow")
        await harness.turn_started(1)
        await bound(turn)

        harness.server.emit(
            "turn/completed",
            {"threadId": harness.server.thread_id, "turn": completed("codex-turn-elsewhere")},
        )
        await asyncio.sleep(0.01)

        assert not turn.settled, "a foreign completion is not this turn's boundary"
        assert harness.session.active_turn_id == turn.turn_id
        assert harness.of(TurnFaulted) == []

        harness.server.message("mine")
        harness.server.complete()

        assert (await asyncio.wait_for(turn.outcome(), 2)).text == "mine"
    finally:
        await harness.stop()


async def test_an_error_naming_a_turn_of_ours_faults_only_that_turn(tmp_path: Path) -> None:
    harness = await start(tmp_path)
    try:
        turn = harness.session.submit("hello")
        await harness.turn_started(1)
        await bound(turn)
        harness.server.emit(
            "error",
            {
                "threadId": harness.server.thread_id,
                "turnId": harness.server.current_turn,
                "willRetry": False,
                "error": {"message": "the model refused"},
            },
        )

        await asyncio.sleep(0.01)

        # meiva:note
        # A turn-scoped failure is the bridge's to act on: it ends that turn's wait and
        # retires nothing else. The session-level failure is the other shape, and this
        # test is what keeps the two from collapsing back into one (#5663).
        # meiva:link base/lm/harness/docs/attribution.meiva.okf.md
        assert [event.turn_id for event in harness.of(TurnFaulted)] == [turn.turn_id]
        assert harness.of(SessionFailed) == [], "one turn failing does not condemn the session"
        assert not turn.settled, "the ticket settles on the turn boundary, not on the fault"
    finally:
        await harness.stop()


async def test_an_error_naming_no_turn_of_ours_is_not_a_fault(tmp_path: Path) -> None:
    delivered: list[UnsolicitedOutput] = []
    harness = await start(tmp_path, unsolicited=delivered.append)
    try:
        turn = harness.session.submit("hello")
        await harness.turn_started(1)
        await bound(turn)
        harness.server.emit(
            "error",
            {
                "threadId": harness.server.thread_id,
                "turnId": "codex-turn-elsewhere",
                "willRetry": False,
                "error": {"message": "usage limit"},
            },
        )
        await asyncio.sleep(0.01)

        assert harness.of(TurnFaulted) == []
        assert [event.text for event in delivered] == ["usage limit"]

        harness.server.message("reply")
        harness.server.complete()

        assert (await asyncio.wait_for(turn.outcome(), 2)).text == "reply"
    finally:
        await harness.stop()


async def test_items_naming_an_unknown_turn_are_not_adopted(tmp_path: Path) -> None:
    harness = await start(tmp_path)
    try:
        turn = harness.session.submit("hello")
        await harness.turn_started(1)
        await bound(turn)
        harness.server.emit(
            "item/agentMessage/delta",
            {
                "threadId": harness.server.thread_id,
                "turnId": "codex-turn-elsewhere",
                "itemId": "msg-x",
                "delta": "LEAK",
            },
        )
        await asyncio.sleep(0.01)

        assert harness.of(TextDelta) == [], "a name we never bound is not ours to adopt"

        harness.server.message("mine")
        harness.server.complete()

        assert (await asyncio.wait_for(turn.outcome(), 2)).text == "mine"
    finally:
        await harness.stop()


async def test_a_notification_from_another_thread_is_ignored(tmp_path: Path) -> None:
    harness = await start(tmp_path)
    try:
        turn = harness.session.submit("hello")
        await harness.turn_started(1)
        harness.server.emit(
            "item/agentMessage/delta",
            {
                "threadId": "thread-elsewhere",
                "turnId": harness.server.current_turn,
                "itemId": "msg-x",
                "delta": "LEAK",
            },
        )
        await asyncio.sleep(0.01)

        assert harness.of(TextDelta) == []

        harness.server.message("mine")
        harness.server.complete()

        assert (await asyncio.wait_for(turn.outcome(), 2)).text == "mine"
    finally:
        await harness.stop()


# meiva:note
# `turn/started` can supply the interrupt target while early activity waits for
# the authoritative response. The receipt leads even when completion arrived first.
# meiva:link base/lm/harness/docs/attribution.meiva.okf.md
async def test_turn_started_binds_the_name_before_the_response(tmp_path: Path) -> None:
    harness = await start(tmp_path, max_buffered_events=4)
    try:
        harness.server.turn_start_gate = asyncio.get_running_loop().create_future()
        harness.server.turn_start_id = "codex-turn-early"
        turn = harness.session.submit("hello")
        await asyncio.sleep(0)
        assert harness.server.params_for("turn/start")

        harness.server.emit(
            "turn/started",
            {
                "threadId": harness.server.thread_id,
                "turn": {"id": "codex-turn-early", "status": "inProgress", "items": []},
            },
        )
        assert turn.provider_turn_id == "codex-turn-early"

        harness.server.emit(
            "item/agentMessage/delta",
            {
                "threadId": harness.server.thread_id,
                "turnId": "codex-turn-early",
                "itemId": "msg-1",
                "delta": "early ",
            },
        )
        for _ in range(5):
            harness.server.delta("extra ", turn_id="codex-turn-early")
        harness.server.command_started(turn_id="codex-turn-early")
        harness.server.usage(input_tokens=7, output_tokens=2, turn_id="codex-turn-early")
        await asyncio.sleep(0.01)
        assert harness.of(TextDelta) == []
        assert harness.of(ToolUse) == []
        assert harness.of(ForegroundAccepted) == []
        assert harness.session.dropped_deltas == 2

        harness.server.message("done", turn_id="codex-turn-early")
        harness.server.complete(turn_id="codex-turn-early")
        assert not turn.settled
        harness.server.turn_start_gate.set_result(None)
        outcome = await asyncio.wait_for(turn.outcome(), 2)
        assert outcome.text == "done"
        assert outcome.usage.input_tokens == 7
        assert [
            type(event)
            for event in harness.events
            if isinstance(event, ForegroundAccepted | TextDelta | ToolUse | TurnCompleted)
        ] == [ForegroundAccepted, TextDelta, TextDelta, TextDelta, ToolUse, TurnCompleted]
        assert harness.of(TextDelta)[0].text == "early "
        assert harness.of(ForegroundAccepted)[0].turn_id == turn.turn_id
    finally:
        await harness.stop()


async def test_rejected_start_never_accepts_a_provisionally_bound_turn(tmp_path: Path) -> None:
    harness = await start(tmp_path)
    try:
        gate: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        harness.server.turn_start_gate = gate
        turn = harness.session.submit("hello")
        await asyncio.sleep(0)
        assert harness.server.params_for("turn/start")
        harness.server.emit(
            "turn/started",
            {
                "threadId": harness.server.thread_id,
                "turn": {"id": "replayed-turn", "status": "inProgress", "items": []},
            },
        )
        harness.server.command_started(turn_id="replayed-turn")
        harness.server.message("foreign", turn_id="replayed-turn")
        harness.server.complete(turn_id="replayed-turn")
        gate.set_exception(JsonRpcError(INVALID_REQUEST, "start rejected"))
        with pytest.raises(SessionClosedError, match="start rejected"):
            _ = await asyncio.wait_for(turn.outcome(), 2)
        assert harness.of(ForegroundAccepted) == []
        assert harness.of(ToolUse) == []
        assert harness.of(AssistantText) == []
        assert harness.of(TurnCompleted) == []
    finally:
        await harness.stop()


async def test_early_activity_follows_the_authoritative_turn_id(tmp_path: Path) -> None:
    harness = await start(tmp_path)
    try:
        gate: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        harness.server.turn_start_gate = gate
        harness.server.turn_start_id = "actual-turn"
        turn = harness.session.submit("hello")
        await asyncio.sleep(0)
        harness.server.emit(
            "turn/started",
            {
                "threadId": harness.server.thread_id,
                "turn": {"id": "replayed-turn", "status": "inProgress", "items": []},
            },
        )
        harness.server.command_started(turn_id="replayed-turn")
        harness.server.delta("foreign", turn_id="replayed-turn")
        harness.server.usage(input_tokens=99, output_tokens=99, turn_id="replayed-turn")
        harness.server.emit(
            "error",
            {
                "threadId": harness.server.thread_id,
                "turnId": "replayed-turn",
                "willRetry": False,
                "error": {"message": "foreign failure"},
            },
        )
        harness.server.complete(turn_id="replayed-turn")
        harness.server.command_started(turn_id="actual-turn", item_id="actual-command")
        harness.server.delta("mine", turn_id="actual-turn")
        harness.server.usage(input_tokens=7, output_tokens=2, turn_id="actual-turn")
        gate.set_result(None)
        await harness.turn_started()
        harness.server.message("done")
        harness.server.complete()
        outcome = await asyncio.wait_for(turn.outcome(), 2)
        assert outcome.text == "done"
        assert outcome.usage.input_tokens == 7
        assert harness.of(TurnFaulted) == []
        assert [event.tool_use_id for event in harness.of(ToolUse)] == ["actual-command"]
        assert [event.text for event in harness.of(TextDelta)] == ["mine"]
        assert len(harness.of(ForegroundAccepted)) == 1
    finally:
        await harness.stop()

from __future__ import annotations

import asyncio
from pathlib import Path

from base.lm.harness.antigravity.tests.support import say, start
from base.lm.harness.events import (
    SessionFailed,
    TextDelta,
    TurnCompleted,
    TurnFaulted,
    UnsolicitedOutput,
)


def result_frame(text: str, *, status: str = "SUCCESS") -> dict[str, object]:
    return {
        "event": "result",
        "result": {
            "conversation_id": "conv-fake",
            "status": status,
            "response": text,
            "duration_seconds": 0.01,
            "num_turns": 1,
            "usage": {
                "input_tokens": 3,
                "output_tokens": 2,
                "thinking_tokens": 0,
                "cache_read_tokens": 0,
                "total_tokens": 5,
            },
        },
    }


# meiva:note
# `agy` names no turns: `init`, `step_update` and `result` carry no request id, so the
# only evidence that a `result` answers something this session asked for is that the
# session wrote a turn out and has not seen it end. These tests push a frame straight
# into the session's frame handler because the CLI cannot be asked to print one on
# demand; everything around the frame (a real turn, a real process) still runs against
# `fake_agy`, which speaks the same NDJSON framing.
# meiva:link base/lm/harness/docs/attribution.meiva.okf.md
async def test_a_result_with_no_turn_in_flight_is_not_a_fault(tmp_path: Path) -> None:
    delivered: list[UnsolicitedOutput] = []
    harness = await start(tmp_path, turns=[say("mine")], unsolicited=delivered.append)
    try:
        harness.session._handle(result_frame("BGDONE"))
        await asyncio.sleep(0.05)

        assert [event.text for event in delivered] == ["BGDONE"]
        assert harness.of(TurnFaulted) == []
        assert harness.of(SessionFailed) == []

        ticket = harness.session.submit("hi")
        assert (await asyncio.wait_for(ticket.outcome(), 5)).text == "mine"
    finally:
        await harness.stop()


async def test_a_late_result_does_not_reopen_a_completed_turn(tmp_path: Path) -> None:
    delivered: list[UnsolicitedOutput] = []
    harness = await start(
        tmp_path, turns=[say("first"), say("second")], unsolicited=delivered.append
    )
    try:
        first = harness.session.submit("one")
        assert (await asyncio.wait_for(first.outcome(), 5)).text == "first"

        harness.session._handle(result_frame("LATE"))
        await asyncio.sleep(0.05)

        assert [event.text for event in delivered] == ["LATE"]
        assert [event.turn_id for event in harness.of(TurnCompleted)] == [first.turn_id]

        second = harness.session.submit("two")
        assert (await asyncio.wait_for(second.outcome(), 5)).text == "second"
    finally:
        await harness.stop()


# meiva:note
# A result that lands mid-turn is indistinguishable from ours at the protocol level, so
# the turn ends either way. What must not happen is the frame's own `response` becoming
# the reply: the text is what this turn streamed, and the frame only fills a gap.
# meiva:link base/lm/harness/docs/attribution.meiva.okf.md
async def test_a_foreign_result_does_not_replace_the_reply_text(tmp_path: Path) -> None:
    harness = await start(tmp_path, turns=[{**say("partial"), "pace": 0.4}])
    try:
        ticket = harness.session.submit("slow")
        for _ in range(500):
            if harness.of(TextDelta):
                break
            await asyncio.sleep(0.002)

        harness.session._handle(result_frame("SOMETHING ELSE"))

        outcome = await asyncio.wait_for(ticket.outcome(), 5)
        assert outcome.text == "partial", "the turn's own streamed text wins"
    finally:
        await harness.stop()

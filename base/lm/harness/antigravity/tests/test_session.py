from __future__ import annotations

from pathlib import Path

from base.lm.claude_sdk.events import (
    AssistantText,
    SessionBound,
    SessionClosed,
    SessionStarted,
    TextDelta,
    TurnCompleted,
)
from base.lm.claude_sdk.registry import FileSessionRegistry
from base.lm.harness.antigravity.tests.support import say, start


async def test_a_turn_streams_text_and_completes(tmp_path: Path) -> None:
    harness = await start(
        tmp_path, turns=[say("hello")], agent_id=7, registry=FileSessionRegistry(tmp_path / "reg")
    )
    try:
        ticket = harness.session.submit("hi")
        outcome = await ticket.outcome()
        assert outcome.text == "hello"
        assert outcome.is_error is False
        assert outcome.subtype == "success"
        started = harness.of(SessionStarted)[0]
        assert started.session_id == "conv-fake"
        assert harness.of(SessionBound)[0].agent_id == 7
        assert harness.of(TextDelta)[0].text == "hello"
        assert harness.of(AssistantText)[0].text == "hello"
        completed = harness.of(TurnCompleted)[0]
        assert completed.turn_id == ticket.turn_id
        assert completed.num_turns == 1
        record = harness.session.record
        assert record is not None and record.session_id == "conv-fake"
    finally:
        await harness.stop()


async def test_close_ends_the_event_stream(tmp_path: Path) -> None:
    harness = await start(tmp_path, turns=[say("bye")])
    await harness.stop()
    assert harness.of(SessionClosed)


async def test_two_turns_reuse_one_process(tmp_path: Path) -> None:
    harness = await start(tmp_path, turns=[say("one"), say("two")])
    try:
        first = await harness.session.submit("a").outcome()
        second = await harness.session.submit("b").outcome()
        assert (first.text, second.text) == ("one", "two")
        assert len(harness.argv_seen()) > 0
        assert "--conversation" not in harness.argv_seen()
    finally:
        await harness.stop()

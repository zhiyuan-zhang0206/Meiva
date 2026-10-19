from __future__ import annotations

from pathlib import Path

from base.lm.harness.antigravity.tests.support import say, start
from base.lm.harness.events import SessionFaulted, TurnCompleted


async def test_a_turn_past_its_deadline_is_interrupted_not_dropped(tmp_path: Path) -> None:
    harness = await start(tmp_path, turns=[{"hang": True}, say("after")], turn_timeout_seconds=0.3)
    try:
        ticket = harness.session.submit("this will hang")
        outcome = await ticket.outcome()
        assert outcome.interrupted is True
        assert outcome.is_error is True
        faults = harness.of(SessionFaulted)
        assert any("exceeded" in fault.error for fault in faults)
        completed = await harness.wait_for(lambda event: isinstance(event, TurnCompleted))
        assert completed.interrupted is True
    finally:
        await harness.stop()


async def test_the_session_serves_the_next_turn_after_a_timeout(tmp_path: Path) -> None:
    harness = await start(tmp_path, turns=[{"hang": True}, say("after")], turn_timeout_seconds=0.3)
    try:
        await harness.session.submit("hang").outcome()
        second = await harness.session.submit("now work").outcome()
        assert second.text == "after"
        assert second.is_error is False
        assert (
            "--conversation" in harness.argv_seen()
            or harness.argv_seen().count("--conversation") == 0
        )
    finally:
        await harness.stop()

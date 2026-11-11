from __future__ import annotations

from pathlib import Path

import pytest

from base.lm.harness.antigravity.session import InboundQueueFullError
from base.lm.harness.antigravity.tests.support import say, start
from base.lm.harness.events import TurnCompleted


async def test_a_second_message_waits_for_the_first_turn_to_finish(tmp_path: Path) -> None:
    slow = say("first")
    slow["pace"] = 0.2
    harness = await start(tmp_path, turns=[slow, say("second")])
    try:
        harness.session.submit("one")
        harness.session.submit("two")
        await harness.wait_until(lambda: len(harness.messages()) == 1)
        assert len(harness.messages()) == 1
        await harness.wait_for(lambda event: isinstance(event, TurnCompleted))
        await harness.wait_until(lambda: len(harness.messages()) == 2)
        assert harness.messages() == ["one", "two"]
    finally:
        await harness.stop()


async def test_a_full_queue_is_refused_not_buffered(tmp_path: Path) -> None:
    slow = say("first")
    slow["pace"] = 0.3
    harness = await start(tmp_path, turns=[slow, say("second")], max_pending_turns=1)
    try:
        harness.session.submit("one")
        harness.session.submit("two")
        with pytest.raises(InboundQueueFullError):
            harness.session.submit("three")
        assert harness.session.pending_turns != ()
    finally:
        await harness.stop()


async def test_an_empty_message_is_rejected(tmp_path: Path) -> None:
    harness = await start(tmp_path, turns=[say("x")])
    try:
        with pytest.raises(ValueError):
            harness.session.submit("   ")
    finally:
        await harness.stop()

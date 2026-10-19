from __future__ import annotations

from pathlib import Path

from base.lm.harness.antigravity.tests.support import say, start


async def test_the_same_key_returns_the_same_ticket(tmp_path: Path) -> None:
    harness = await start(tmp_path, turns=[say("once")])
    try:
        first = harness.session.submit("do it", key="k1")
        second = harness.session.submit("do it", key="k1")
        assert second is first
        assert second.deduplicated is False
        await first.outcome()
        await harness.wait_until(lambda: len(harness.messages()) == 1)
    finally:
        await harness.stop()


async def test_a_completed_key_replays_the_recorded_result(tmp_path: Path) -> None:
    harness = await start(tmp_path, turns=[say("once")])
    try:
        first = harness.session.submit("do it", key="k1")
        outcome = await first.outcome()
        replay = harness.session.submit("different text", key="k1")
        assert replay.deduplicated is True
        assert replay.turn_id == first.turn_id
        replayed = await replay.outcome()
        assert replayed.text == outcome.text
        await harness.wait_until(lambda: len(harness.messages()) == 1)
        assert harness.messages() == ["do it"]
    finally:
        await harness.stop()

from __future__ import annotations

from pathlib import Path

from base.lm.harness.events import TextDelta, TurnCompleted
from base.lm.harness.kimi.tests.support import start


# meiva:note
# `session/update` carries no turn id: the protocol attributes an update by the
# session it names and nothing else, so one the runtime sends on its own — a
# background command finishing, a sub-session reporting — can only be recognised by
# there being no turn in flight. It is dropped and counted there rather than carried
# into the next turn, which is what used to poison a turn that had nothing to do with
# it (#5664). Mid-turn the two are indistinguishable; see the adapter doc.
# meiva:link base/lm/harness/docs/attribution.meiva.okf.md
async def test_an_update_with_no_turn_in_flight_is_counted_not_delivered(
    tmp_path: Path,
) -> None:
    harness = await start(tmp_path)
    try:
        await harness.connection.emit(
            {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "BGDONE"}}
        )
        await harness.settle()

        assert harness.session.unclaimed_updates == 1
        assert harness.of(TextDelta) == []
        assert harness.of(TurnCompleted) == []

        ticket = harness.session.submit("hi")
        assert (await ticket.outcome()).text == "echo:hi"
        assert harness.session.unclaimed_updates == 1
    finally:
        await harness.stop()

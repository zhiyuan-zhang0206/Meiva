from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from pathlib import Path

from base.lm.harness import BridgeEvent, ContextCompacted, SessionStarted
from base.lm.harness.delivery import HarnessInput
from base.lm.harness.events import AckSignal, InputInContext, InputReceived
from base.lm.harness.handoff import compaction_reminder
from base.lm.harness.pump import EventPump
from base.lm.harness.tests.test_bridge import FakeSession, handoff, make_bridge


async def _stream(source: asyncio.Queue[BridgeEvent | None]) -> AsyncIterator[BridgeEvent]:
    while (event := await source.get()) is not None:
        yield event


async def test_a_subscriber_sees_only_what_the_pump_reads_after_it_subscribed() -> None:
    source: asyncio.Queue[BridgeEvent | None] = asyncio.Queue()
    seen: list[BridgeEvent] = []
    pump = EventPump(_stream(source), observe=seen.append)
    task = asyncio.create_task(pump.run())

    source.put_nowait(ContextCompacted())
    await asyncio.sleep(0)
    late = pump.subscribe()
    source.put_nowait(SessionStarted(session_id="s", model=None, cwd=None))
    source.put_nowait(None)
    await task

    assert [type(event) for event in seen] == [ContextCompacted, SessionStarted]
    assert [type(event) async for event in late] == [SessionStarted]
    assert pump.ended
    assert await pump.subscribe().get() is None


class DeliveringSession(FakeSession):
    # meiva:note
    # A session that answers a delivery with the two acknowledgements, the way an adapter does, and
    # lets a test inject an event while nothing waits for a turn.
    def __init__(self) -> None:
        super().__init__()
        self.delivered: list[Sequence[HarnessInput]] = []

    async def deliver(self, inputs: Sequence[HarnessInput]) -> None:
        self.delivered.append(inputs)
        for entry in inputs:
            self._queue.append(InputReceived(entry.key, AckSignal.WEAK, 1))
            self._queue.append(InputInContext(entry.key, "turn-1", 1))


async def test_a_compaction_seen_while_idle_puts_the_reminder_on_the_next_delivery() -> None:
    session = DeliveringSession()
    instructions = Path("/agents/6984/instructions.md")
    doc = handoff().with_reference_file(instructions)
    bridge = make_bridge(session, handoff=doc)
    subscription = await bridge.subscribe()

    await bridge.deliver([HarnessInput(key="inbound-1", text="one")])
    session._queue.append(ContextCompacted())
    async with asyncio.timeout(2):
        while not isinstance(await subscription.get(), ContextCompacted):
            pass
    await bridge.deliver([HarnessInput(key="inbound-2", text="two")])
    await bridge.deliver([HarnessInput(key="inbound-3", text="three")])

    assert [batch[0].text for batch in session.delivered] == [
        f"{doc.render().strip()}\n\none",
        f"{compaction_reminder(instructions)}\n\ntwo",
        "three",
    ]

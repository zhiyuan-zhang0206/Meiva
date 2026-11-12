from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Sequence
from pathlib import Path

from base.clock.tests.fakes import clock_over
from base.clock.tests.manual_timers import ManualTimers
from base.lm.harness import (
    AssistantText,
    BridgeEvent,
    ContextCompacted,
    SessionStarted,
    TurnCompleted,
    TurnOrigin,
    TurnStarted,
    Usage,
)
from base.lm.harness.delivery import HarnessInput
from base.lm.harness.events import (
    NO_TURN,
    AckSignal,
    HarnessCause,
    InputInContext,
    InputReceived,
    SubagentText,
)
from base.lm.harness.handoff import compaction_reminder
from base.lm.harness.pump import ORPHAN_REWAKE_SECONDS, EventPump
from base.lm.harness.tests.test_bridge import FakeSession, handoff, make_bridge


async def _stream(source: asyncio.Queue[BridgeEvent | None]) -> AsyncIterator[BridgeEvent]:
    while (event := await source.get()) is not None:
        yield event


async def test_a_subscriber_sees_only_what_the_pump_reads_after_it_subscribed() -> None:
    source: asyncio.Queue[BridgeEvent | None] = asyncio.Queue()
    seen: list[BridgeEvent] = []
    pump = _pump(source, observe=seen.append)
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


def _pump(
    source: asyncio.Queue[BridgeEvent | None],
    *,
    observe: Callable[[BridgeEvent], None] = lambda _e: None,
    own_turn: Callable[[], None] | None = None,
    timers: ManualTimers | None = None,
) -> EventPump:
    return EventPump(
        _stream(source),
        observe=observe,
        own_turn=own_turn,
        clock=clock_over(timers or ManualTimers()),
    )


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

    await bridge.deliver([HarnessInput.of(key="inbound-1", text="one")])
    session._queue.append(ContextCompacted())
    async with asyncio.timeout(2):
        while not isinstance(await subscription.get(), ContextCompacted):
            pass
    await bridge.deliver([HarnessInput.of(key="inbound-2", text="two")])
    await bridge.deliver([HarnessInput.of(key="inbound-3", text="three")])

    assert [batch[0].text for batch in session.delivered] == [
        f"{doc.render().strip()}\n\none",
        f"{compaction_reminder(instructions)}\n\ntwo",
        "three",
    ]


async def test_a_compaction_turn_between_turns_puts_the_reminder_on_the_very_next_delivery() -> (
    None
):
    # meiva:note
    # The shape a real Codex showed after `thread/compact/start`: a turn the server began itself, the compaction
    # item some seconds in, the turn's end, and the next input arriving right behind it. The reminder
    # rides that next input, not the one after.
    # meiva:link base/lm/harness/docs/delivery.md
    session = DeliveringSession()
    instructions = Path("/agents/6984/instructions.md")
    doc = handoff().with_reference_file(instructions)
    bridge = make_bridge(session, handoff=doc)
    subscription = await bridge.subscribe()
    await bridge.deliver([HarnessInput.of(key="inbound-1", text="one")])

    session._queue.append(_own("compaction"))
    session._queue.append(ContextCompacted(turn_id="compaction"))
    session._queue.append(
        TurnCompleted(
            turn_id="compaction",
            session_id=None,
            text="",
            subtype="completed",
            is_error=False,
            interrupted=False,
            stop_reason=None,
            num_turns=1,
            duration_ms=1,
            usage=Usage(),
            cost_usd=None,
            cumulative_cost_usd=None,
            permission_denials=(),
            generation=1,
        )
    )
    async with asyncio.timeout(2):
        while not isinstance(await subscription.get(), TurnCompleted):
            pass
    await bridge.deliver([HarnessInput.of(key="inbound-2", text="two")])
    await bridge.deliver([HarnessInput.of(key="inbound-3", text="three")])

    assert [batch[0].text for batch in session.delivered][1:] == [
        f"{compaction_reminder(instructions)}\n\ntwo",
        "three",
    ]


def _own(turn_id: str) -> TurnStarted:
    return TurnStarted(turn_id, TurnOrigin.HARNESS, (), 1, cause=HarnessCause.SCHEDULED_WAKE)


async def test_a_turn_the_cli_begins_with_nobody_subscribed_is_kept_and_primes_the_next_subscriber() -> (
    None
):
    source: asyncio.Queue[BridgeEvent | None] = asyncio.Queue()
    woken: list[str] = []
    pump = _pump(source, own_turn=lambda: woken.append("w"))
    task = asyncio.create_task(pump.run())

    source.put_nowait(_own("w1"))
    source.put_nowait(AssistantText("w1", "tick"))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert woken == ["w"] and pump.has_unpresented_turn

    late = pump.subscribe()
    first = await late.get()
    assert isinstance(first, TurnStarted)
    assert not pump.has_unpresented_turn, "once read, the turn is presented"
    assert isinstance(await late.get(), AssistantText)
    source.put_nowait(AssistantText("w1", "tock"))
    source.put_nowait(None)
    await task
    assert [type(event) async for event in late] == [AssistantText]


async def test_a_turn_read_by_a_live_subscriber_is_not_offered_to_the_next_one() -> None:
    source: asyncio.Queue[BridgeEvent | None] = asyncio.Queue()
    pump = _pump(source)
    task = asyncio.create_task(pump.run())
    live = pump.subscribe()

    source.put_nowait(_own("w1"))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert pump.has_unpresented_turn, "offered is not read"
    assert isinstance(await live.get(), TurnStarted)
    assert not pump.has_unpresented_turn

    live.close()
    again = pump.subscribe()
    assert not again.unread
    source.put_nowait(None)
    await task


async def test_a_turn_unread_when_its_subscriber_left_is_still_presented() -> None:
    source: asyncio.Queue[BridgeEvent | None] = asyncio.Queue()
    pump = _pump(source)
    task = asyncio.create_task(pump.run())
    leaving = pump.subscribe()

    source.put_nowait(_own("w1"))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    leaving.close()

    successor = pump.subscribe()
    assert isinstance(await successor.get(), TurnStarted)
    source.put_nowait(None)
    await task


def _said(text: str, turn_id: str = NO_TURN) -> SubagentText:
    return SubagentText(turn_id, "call", None, text)


async def test_subagent_activity_with_no_turn_is_kept_for_the_owner_and_begins_no_turn() -> None:
    source: asyncio.Queue[BridgeEvent | None] = asyncio.Queue()
    woken: list[str] = []
    pump = _pump(source, own_turn=lambda: woken.append("w"))
    task = asyncio.create_task(pump.run())

    burst = [_said(text) for text in ("one", "two", "three")]
    for event in burst:
        source.put_nowait(event)
    source.put_nowait(_said("in a turn", "t1"))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert woken == ["w"], "a burst of N frames is one wake"
    assert not pump.has_unpresented_turn, "it is not a turn"

    assert pump.orphans() == burst
    pump.acknowledge_orphans(2)
    assert pump.orphans() == burst[2:], "only what was acknowledged is gone"
    pump.acknowledge_orphans(1)
    assert pump.orphans() == []
    source.put_nowait(_said("later"))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert woken == ["w", "w"], "after the owner took them, the next frame wakes it again"
    source.put_nowait(None)
    await task


async def test_a_wake_that_was_refused_is_repeated_after_the_interval_not_before() -> None:
    source: asyncio.Queue[BridgeEvent | None] = asyncio.Queue()
    woken: list[str] = []
    timers = ManualTimers()
    pump = _pump(source, own_turn=lambda: woken.append("w"), timers=timers)
    task = asyncio.create_task(pump.run())

    source.put_nowait(_said("one"))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert woken == ["w"]
    timers.advance(ORPHAN_REWAKE_SECONDS - 1)
    source.put_nowait(_said("two"))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert woken == ["w"], "inside the interval a kept event does not wake again"
    timers.advance(2)
    source.put_nowait(_said("three"))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert woken == ["w", "w"], "the owner never took them: wake it again"
    assert len(pump.orphans()) == 3, "nothing was lost meanwhile"
    source.put_nowait(None)
    await task


async def test_subagent_activity_is_read_live_by_a_subscriber_and_wakes_nobody() -> None:
    source: asyncio.Queue[BridgeEvent | None] = asyncio.Queue()
    woken: list[str] = []
    pump = _pump(source, own_turn=lambda: woken.append("w"))
    task = asyncio.create_task(pump.run())
    live = pump.subscribe()

    first, second = _said("one"), _said("two")
    source.put_nowait(first)
    source.put_nowait(second)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert woken == [], "a run in flight reads it itself"
    assert await live.get() is first
    live.close()
    assert woken == ["w"], "a subscription that leaves with some unread wakes the owner"
    assert pump.orphans() == [second]
    pump.acknowledge_orphans(1)

    drained = pump.subscribe()
    source.put_nowait(_said("three"))
    await asyncio.sleep(0)
    assert (await drained.get()) is not None
    drained.close()
    assert woken == ["w"], "read events are not left behind"
    source.put_nowait(None)
    await task


async def test_a_turn_begun_by_our_own_input_is_never_kept_or_announced() -> None:
    source: asyncio.Queue[BridgeEvent | None] = asyncio.Queue()
    woken: list[str] = []
    pump = _pump(source, own_turn=lambda: woken.append("w"))
    task = asyncio.create_task(pump.run())

    source.put_nowait(TurnStarted("t1", TurnOrigin.INPUT, ("k",), 1))
    source.put_nowait(None)
    await task

    assert woken == [] and not pump.has_unpresented_turn

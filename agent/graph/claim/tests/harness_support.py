# meiva:note
# A scripted turn engine: the graph's view of an external harness, without a process. `deliver()` answers with the events a script produces for that batch, the way a harness reports what became of its inputs. The default script is the common case: the batch starts a turn, every input enters its context, the reply streams, the turn ends.
# meiva:link agent/graph/docs/harness-turn.md

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from contextlib import suppress

from base.api_contracts.agent_contract import Harness
from base.lm.harness import (
    BridgeEvent,
    HarnessInput,
    InputInContext,
    InputRejected,
    TurnCompleted,
    TurnOrigin,
    TurnStarted,
    Usage,
)

__all__ = ["FakeEngine", "FakeSubscription", "completed", "in_a_turn"]

Script = Callable[[Sequence[HarnessInput]], Sequence[BridgeEvent]]


def completed(
    turn_id: str,
    text: str,
    *,
    usage: Usage | None = None,
    interrupted: bool = False,
    is_error: bool = False,
) -> TurnCompleted:
    return TurnCompleted(
        turn_id=turn_id,
        session_id=None,
        text=text,
        subtype="success",
        is_error=is_error,
        interrupted=interrupted,
        stop_reason=None,
        num_turns=1,
        duration_ms=1,
        usage=usage or Usage(input_tokens=7, output_tokens=3),
        cost_usd=0.01,
        cumulative_cost_usd=None,
        permission_denials=(),
        generation=1,
    )


# meiva:note
# The events of one ordinary turn that took `keys`: it starts, each input enters the context, the
# given activity streams, the turn ends with `reply`.
def in_a_turn(
    keys: Sequence[str],
    reply: str,
    *,
    turn_id: str = "t1",
    activity: Sequence[BridgeEvent] = (),
    usage: Usage | None = None,
    interrupted: bool = False,
) -> list[BridgeEvent]:
    return [
        TurnStarted(turn_id, TurnOrigin.INPUT, tuple(keys), 1),
        *(InputInContext(key, turn_id, 1) for key in keys),
        *activity,
        completed(turn_id, reply, usage=usage, interrupted=interrupted),
    ]


class FakeSubscription:
    def __init__(self, engine: FakeEngine) -> None:
        self._engine = engine
        self.queue: asyncio.Queue[BridgeEvent | None] = asyncio.Queue()

    async def get(self) -> BridgeEvent | None:
        return await self.queue.get()

    def close(self) -> None:
        self._engine.subscriptions.discard(self)


class FakeEngine:
    def __init__(
        self,
        reply: str = "MEIVA_DISPLAY_TO_USER\nthe answer",
        usage: Usage | None = None,
        events: Sequence[BridgeEvent] = (),
        interrupted: bool = False,
        script: Script | None = None,
    ) -> None:
        self.harness = Harness.CLAUDE_CODE
        self.model = "claude-sonnet-5-5"
        self.exit_code: int | None = None
        self.delivered: list[list[HarnessInput]] = []
        self.interrupts = 0
        self.retired: list[str] = []
        self.closed = False
        self.subscriptions: set[FakeSubscription] = set()
        self._inbound = asyncio.Event()

        def ordinary(inputs: Sequence[HarnessInput]) -> Sequence[BridgeEvent]:
            return in_a_turn(
                [item.key for item in inputs],
                reply,
                activity=events,
                usage=usage,
                interrupted=interrupted,
            )

        self._script: Script = script or ordinary

    @property
    def submitted(self) -> list[tuple[str, str]]:
        return [(i.text, i.key) for batch in self.delivered for i in batch]

    async def subscribe(self) -> FakeSubscription:
        subscription = FakeSubscription(self)
        self.subscriptions.add(subscription)
        return subscription

    def emit(self, *events: BridgeEvent) -> None:
        for subscription in tuple(self.subscriptions):
            for event in events:
                subscription.queue.put_nowait(event)

    async def deliver(self, inputs: Sequence[HarnessInput]) -> None:
        self.delivered.append(list(inputs))
        self.emit(*self._script(inputs))

    async def interrupt(self) -> object:
        self.interrupts += 1
        return object()

    async def retire(self, reason: str) -> None:
        self.retired.append(reason)

    async def close(self, *, reason: str = "closed") -> None:
        self.closed = True

    def notify_inbound(self) -> None:
        self._inbound.set()

    async def wait_inbound(self, timeout: float) -> None:
        with suppress(TimeoutError):
            await asyncio.wait_for(self._inbound.wait(), timeout)
        self._inbound.clear()


def rejecting(reason: str = "no active turn to steer") -> Script:
    def script(inputs: Sequence[HarnessInput]) -> Sequence[BridgeEvent]:
        return [InputRejected(i.key, reason, 1) for i in inputs]

    return script

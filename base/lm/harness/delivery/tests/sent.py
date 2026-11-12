# meiva:note
# What the adapter tests use to hand one input to a session and follow it by its events, the only way an
# input's fate is reported: `send` delivers it, `Sent.turn_id` is the turn it entered the context of, and
# `Sent.outcome()` waits for that turn to complete. A harness here is anything with an `events` list the
# session's stream is collected into and a `session` that takes `deliver`.
# meiva:link base/lm/harness/docs/delivery.md
from __future__ import annotations

import asyncio
import itertools
from collections.abc import Callable
from typing import Any

from base.lm.harness.delivery import HarnessInput, ImageBlock
from base.lm.harness.events import BridgeEvent, InputInContext, TurnCompleted, TurnStarted

_keys = itertools.count(1)


class Sent:
    def __init__(self, harness: Any, key: str) -> None:
        self.harness = harness
        self.key = key
        self.delivery: asyncio.Task[None] | None = None

    @property
    def turn_id(self) -> str:
        for event in list(self.harness.events):
            if isinstance(event, InputInContext) and event.key == self.key:
                return event.turn_id
            if isinstance(event, TurnStarted) and self.key in event.keys:
                return event.turn_id
        active = self.harness.session.active_turn_id
        if active is not None:
            return active
        raise AssertionError(f"input {self.key!r} has not started or entered a turn yet")

    @property
    def provider_turn_id(self) -> str | None:
        return self.harness.session.active_provider_turn_id

    @property
    def settled(self) -> bool:
        turn_id = self.turn_id
        return any(
            isinstance(e, TurnCompleted) and e.turn_id == turn_id for e in list(self.harness.events)
        )

    async def started(self, timeout: float = 2.0) -> None:
        await self._until(lambda e: isinstance(e, TurnStarted) and self.key in e.keys, timeout)

    async def _until(self, found: Callable[[BridgeEvent], bool], timeout: float) -> BridgeEvent:
        async with asyncio.timeout(timeout):
            while True:
                for event in list(self.harness.events):
                    if found(event):
                        return event
                await asyncio.sleep(0.002)

    async def outcome(self, timeout: float = 2.0) -> TurnCompleted:
        context = await self._until(
            lambda e: (
                (isinstance(e, InputInContext) and e.key == self.key)
                or (isinstance(e, TurnStarted) and self.key in e.keys)
            ),
            timeout,
        )
        if not isinstance(context, (InputInContext, TurnStarted)):
            raise TypeError("unreachable: the predicate selects these two")
        turn_id = context.turn_id
        done = await self._until(
            lambda e: isinstance(e, TurnCompleted) and e.turn_id == turn_id, timeout
        )
        if not isinstance(done, TurnCompleted):
            raise TypeError("unreachable: the predicate selects TurnCompleted")
        return done


async def send(
    harness: Any, text: str, *, key: str | None = None, images: tuple[ImageBlock, ...] = ()
) -> Sent:
    key = key or f"input-{next(_keys)}"
    await harness.session.deliver([HarnessInput.of(key=key, text=text, images=images)])
    await asyncio.sleep(0.005)
    return Sent(harness, key)


# meiva:note
# For a test that holds the CLI's answer back on purpose: `deliver` does not return until the CLI has answered the
# start of a turn, so the delivery runs as a task and the test keeps control while it waits.
# meiva:link base/lm/harness/docs/delivery.md
def send_nowait(harness: Any, text: str, *, key: str | None = None) -> Sent:
    key = key or f"input-{next(_keys)}"
    sent = Sent(harness, key)
    sent.delivery = asyncio.create_task(
        harness.session.deliver([HarnessInput.of(key=key, text=text)])
    )
    return sent

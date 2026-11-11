from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable

from base.lm.harness.events import BridgeEvent
from base.log import logger

# meiva:note
# The one reader of a session's event stream. It lives as long as the session does, not as long as a
# turn: an event that arrives while nothing waits for a turn (a CLI-started turn, a late
# acknowledgement, a background-task report) is still read, observed by the owner and offered to every
# current subscriber instead of piling up in the session's own queue. A subscriber sees only what the
# pump reads after it subscribed, so a waiter subscribes before it hands anything to the session.
# meiva:link base/lm/harness/docs/delivery.md


class Subscription:
    # meiva:note
    # `get()` returns the next event, or None once the stream has ended. It never raises for the
    # stream's end: the subscriber decides what an ended stream means for what it was waiting on.
    def __init__(self, pump: EventPump) -> None:
        self._pump = pump
        self._queue: asyncio.Queue[BridgeEvent | None] = asyncio.Queue()

    def offer(self, event: BridgeEvent | None) -> None:
        self._queue.put_nowait(event)

    async def get(self) -> BridgeEvent | None:
        event = await self._queue.get()
        if event is None:
            self._queue.put_nowait(None)
        return event

    def close(self) -> None:
        self._pump.unsubscribe(self)

    async def __aiter__(self) -> AsyncIterator[BridgeEvent]:
        while (event := await self.get()) is not None:
            yield event


class EventPump:
    def __init__(
        self,
        events: AsyncIterator[BridgeEvent],
        *,
        observe: Callable[[BridgeEvent], None],
    ) -> None:
        self._events = events
        self._observe = observe
        self._subscriptions: set[Subscription] = set()
        self._ended = False

    @property
    def ended(self) -> bool:
        return self._ended

    def subscribe(self) -> Subscription:
        subscription = Subscription(self)
        if self._ended:
            subscription.offer(None)
        else:
            self._subscriptions.add(subscription)
        return subscription

    def unsubscribe(self, subscription: Subscription) -> None:
        self._subscriptions.discard(subscription)

    async def run(self) -> None:
        try:
            async for event in self._events:
                self._observe(event)
                for subscription in tuple(self._subscriptions):
                    subscription.offer(event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("harness event pump stopped: {error}", error=repr(exc))
        finally:
            self._ended = True
            for subscription in tuple(self._subscriptions):
                subscription.offer(None)
            self._subscriptions.clear()

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable

from base.lm.harness.events import (
    NO_TURN,
    BridgeEvent,
    SubagentText,
    ToolResult,
    ToolUse,
    TurnOrigin,
    TurnStarted,
)
from base.log import logger

# meiva:note
# The one reader of a session's event stream. It lives as long as the session does, not as long as a
# turn: an event that arrives while nothing waits for a turn (a CLI-started turn, a late
# acknowledgement, a background-task report) is still read, observed by the owner and offered to every
# current subscriber instead of piling up in the session's own queue. A subscriber sees only what the
# pump reads after it subscribed, so a waiter subscribes before it hands anything to the session.
#
# A turn the CLI began on its own (`TurnStarted(origin=HARNESS)`) is the exception: nobody may be
# subscribed when it begins (the agent is idle), yet it has to be presented, exactly once. The pump keeps
# that turn's events from its start until a subscriber has read its `TurnStarted`, calls `own_turn` so
# the owner wakes the agent, and primes whoever subscribes next with what the turn has produced so far.
# Read means read: an event still unread in a subscription that closed is not a presented turn.
#
# A sub-agent's activity with no turn (`turn_id == NO_TURN`: Codex's child thread speaking after its
# parent's turn ended) is kept too, so it reaches the timeline under the call that started the sub-agent.
# It begins no turn and is not a turn's work: the owner takes it (`take_orphans`) and writes it, and a run
# that is subscribed reads it live instead. The owner is woken for the first event kept while nobody is
# subscribed and nothing is waiting to be taken, so a burst is one wake; a subscription that leaves with
# some of it unread wakes the owner again.
# meiva:link base/lm/harness/docs/delivery.md


class Subscription:
    # meiva:note
    # `get()` returns the next event, or None once the stream has ended. It never raises for the
    # stream's end: the subscriber decides what an ended stream means for what it was waiting on.
    def __init__(self, pump: EventPump) -> None:
        self._pump = pump
        self._queue: asyncio.Queue[BridgeEvent | None] = asyncio.Queue()
        self._unread = 0

    def offer(self, event: BridgeEvent | None) -> None:
        if event is not None:
            self._unread += 1
        self._queue.put_nowait(event)

    @property
    def unread(self) -> bool:
        return self._unread > 0

    async def get(self) -> BridgeEvent | None:
        event = await self._queue.get()
        if event is None:
            self._queue.put_nowait(None)
        else:
            self._unread -= 1
            self._pump.read(event)
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
        own_turn: Callable[[], None] | None = None,
    ) -> None:
        self._events = events
        self._observe = observe
        self._own_turn = own_turn
        self._own: dict[str, list[BridgeEvent]] = {}
        self._orphans: list[BridgeEvent] = []
        self._subscriptions: set[Subscription] = set()
        self._ended = False

    @property
    def ended(self) -> bool:
        return self._ended

    @property
    def has_unpresented_turn(self) -> bool:
        return bool(self._own)

    def take_orphans(self) -> list[BridgeEvent]:
        taken, self._orphans = self._orphans, []
        return taken

    def subscribe(self) -> Subscription:
        subscription = Subscription(self)
        for events in self._own.values():
            for event in events:
                subscription.offer(event)
        for event in self._orphans:
            subscription.offer(event)
        if self._ended:
            subscription.offer(None)
        else:
            self._subscriptions.add(subscription)
        return subscription

    def read(self, event: BridgeEvent) -> None:
        if isinstance(event, TurnStarted) and event.origin is TurnOrigin.HARNESS:
            _ = self._own.pop(event.turn_id, None)
        elif self._orphans:
            self._orphans = [kept for kept in self._orphans if kept is not event]

    def _keep_own_turn(self, event: BridgeEvent) -> bool:
        if isinstance(event, SubagentText | ToolUse | ToolResult) and event.turn_id == NO_TURN:
            began = not self._orphans and not self._subscriptions
            self._orphans.append(event)
            return began
        if isinstance(event, TurnStarted):
            if event.origin is not TurnOrigin.HARNESS:
                return False
            self._own[event.turn_id] = [event]
            return True
        turn_id = getattr(event, "turn_id", None)
        if isinstance(turn_id, str) and turn_id in self._own:
            self._own[turn_id].append(event)
        return False

    def unsubscribe(self, subscription: Subscription) -> None:
        self._subscriptions.discard(subscription)
        if self._orphans and not self._subscriptions and self._own_turn is not None:
            self._own_turn()

    async def run(self) -> None:
        try:
            async for event in self._events:
                self._observe(event)
                began = self._keep_own_turn(event)
                for subscription in tuple(self._subscriptions):
                    subscription.offer(event)
                if began and self._own_turn is not None:
                    self._own_turn()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("harness event pump stopped: {error}", error=repr(exc))
        finally:
            self._ended = True
            self._own.clear()
            self._orphans.clear()
            for subscription in tuple(self._subscriptions):
                subscription.offer(None)
            self._subscriptions.clear()

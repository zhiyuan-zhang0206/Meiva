# meiva:note
# A subscriber on the cluster's live-events channel, as a frontend or an agent host would be.
#
# The gateway announces committed changes on one Redis channel (`AgentUpdated`, `NoticePosted`,
# `NoticeResolved`, ...). A test that wants to know what was announced subscribes here instead of
# replacing the publishing function: the real publish runs, the event travels through the test's
# Redis, and the recorder reads it back. `react` runs on the subscriber's thread the moment an
# event arrives, which is how a consumer reconciles from the authoritative store: a test uses it to
# read what the store showed when the hint was published.

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from base.events.live.bus import EventBus

__all__ = ["LiveEventRecorder", "RecordedEvent", "recording_live_events"]

_POLL_S = 0.02


@dataclass(frozen=True)
class RecordedEvent:
    payload: dict[str, Any]
    reaction: Any

    @property
    def role(self) -> str:
        return str(self.payload["role"])


class LiveEventRecorder:
    def __init__(self, events: list[RecordedEvent]) -> None:
        self._events = events

    # meiva:note
    # The events of one role, in arrival order, as recorded so far.
    def events(self, role: str) -> list[RecordedEvent]:
        return [event for event in list(self._events) if event.role == role]

    # meiva:note
    # Wait until `count` events of `role` have arrived; fail if they do not in time.
    def settle(self, role: str, count: int, *, timeout_s: float = 5.0) -> list[RecordedEvent]:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            found = self.events(role)
            if len(found) >= count:
                return found
            time.sleep(_POLL_S)
        raise AssertionError(f"expected {count} {role!r} events, saw {len(self.events(role))}")

    # meiva:note
    # Let the channel go quiet, so an event that must not be announced had time to arrive.
    def quiet(self, *, for_s: float = 0.3) -> None:
        time.sleep(for_s)


# meiva:note
# Subscribe to the bus's events channel for the block and record what arrives.
@contextmanager
def recording_live_events(
    bus: EventBus, *, react: Callable[[dict[str, Any]], Any] | None = None
) -> Generator[LiveEventRecorder]:
    events: list[RecordedEvent] = []
    stop = threading.Event()
    ready = threading.Event()
    redis = bus.sync_redis(decode_responses=True)
    pubsub = redis.pubsub()  # pyright: ignore[reportUnknownMemberType]
    pubsub.subscribe(bus.channel)

    def listen() -> None:
        while not stop.is_set():
            # meiva:note
            # The redis-py client and its pubsub are untyped. The annotated shape is the one the untyped call returns.
            message: dict[str, Any] | None = pubsub.get_message(timeout=_POLL_S)  # pyright: ignore[reportUnknownMemberType, reportAssignmentType]
            if message is None:
                continue
            if message["type"] == "subscribe":
                ready.set()
            elif message["type"] == "message":
                payload = json.loads(str(message["data"]))
                events.append(RecordedEvent(payload, react(payload) if react else None))

    thread = threading.Thread(target=listen, name="live-event-recorder")
    thread.start()
    try:
        if not ready.wait(timeout=5.0):
            raise AssertionError("the live-events subscription was not confirmed")
        yield LiveEventRecorder(events)
    finally:
        stop.set()
        thread.join()
        pubsub.close()  # pyright: ignore[reportUnknownMemberType]
        redis.close()

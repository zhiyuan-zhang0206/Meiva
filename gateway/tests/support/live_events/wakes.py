# meiva:note
# A subscriber on one agent's inbound wake channel, as the agent's host would be.
#
# `publish_inbound_wake` publishes `"0"` on the agent's inbound channel to wake an idle host. A
# test that wants to know whether a wake was published listens on that channel through the test's
# Redis instead of replacing the publisher: the real publish runs and the subscriber counts what
# arrives.

from __future__ import annotations

import threading
import time
from collections.abc import Generator
from contextlib import contextmanager
from typing import cast

from base.cluster.derive import inbound_channel
from base.events.live.bus import EventBus

__all__ = ["WakeLog", "recording_wakes"]

_POLL_S = 0.02


class WakeLog:
    def __init__(self, wakes: list[str]) -> None:
        self._wakes = wakes

    def count(self) -> int:
        return len(self._wakes)

    # meiva:note
    # Wait until `count` wakes have arrived; fail the test if they do not in time.
    def settle(self, count: int, *, timeout_s: float = 5.0) -> None:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self.count() >= count:
                return
            time.sleep(_POLL_S)
        raise AssertionError(f"expected {count} wakes, saw {self.count()}")

    # meiva:note
    # Let the channel go quiet, so a wake that must not be published had time to arrive.
    def quiet(self, *, for_s: float = 0.3) -> None:
        time.sleep(for_s)


# meiva:note
# Subscribe to `agent_id`'s inbound wake channel for the block and record what arrives.
@contextmanager
def recording_wakes(bus: EventBus, agent_id: int) -> Generator[WakeLog]:
    wakes: list[str] = []
    stop = threading.Event()
    ready = threading.Event()
    redis = bus.sync_redis(decode_responses=True)
    pubsub = redis.pubsub()  # pyright: ignore[reportUnknownMemberType]
    pubsub.subscribe(inbound_channel(agent_id, bus.channel))

    def listen() -> None:
        while not stop.is_set():
            # meiva:note
            # The redis-py client and its pubsub are untyped. The value comes from an untyped third-party call.
            message = pubsub.get_message(timeout=_POLL_S)  # pyright: ignore[reportUnknownMemberType]
            if message is None:
                continue
            if message["type"] == "subscribe":
                ready.set()
            elif message["type"] == "message":
                data = cast(object, message["data"])
                wakes.append(str(data))

    thread = threading.Thread(target=listen, name="wake-recorder")
    thread.start()
    try:
        if not ready.wait(timeout=5.0):
            raise AssertionError("the wake subscription was not confirmed")
        yield WakeLog(wakes)
    finally:
        stop.set()
        thread.join()
        pubsub.close()  # pyright: ignore[reportUnknownMemberType]
        redis.close()

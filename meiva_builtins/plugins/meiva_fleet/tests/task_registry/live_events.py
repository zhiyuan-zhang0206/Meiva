# meiva:note
# The live events a task operation published, read back from the cluster Redis the test provisions:
# a subscription opened before the operation collects what arrived on the events channel by the time
# the block ends.
import json
import threading
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager
from typing import Any, cast

from base.events.live.bus import EventBus


@contextmanager
def recorded_events(bus: EventBus) -> Generator[list[dict[str, Any]]]:
    received: list[dict[str, Any]] = []
    client = bus.sync_redis(decode_responses=True)
    subscription: Any = cast("Any", client).pubsub()
    try:
        subscription.subscribe(bus.channel)
        confirmation = subscription.get_message(timeout=2.0)
        assert confirmation is not None and confirmation["type"] == "subscribe"
        yield received
        while (message := subscription.get_message(timeout=0.5)) is not None:
            if message["type"] == "message":
                received.append(json.loads(str(message["data"])))
    finally:
        subscription.close()
        client.close()


# meiva:note
# Each live event published while the block runs, with what `snapshot()` read the moment the event
# arrived: a hint that names committed work must find the work visible to a separate connection.
@contextmanager
def snapshots_at_events[T](
    bus: EventBus, snapshot: Callable[[], T]
) -> Generator[list[tuple[dict[str, Any], T]]]:
    seen: list[tuple[dict[str, Any], T]] = []
    client = bus.sync_redis(decode_responses=True)
    subscription: Any = cast("Any", client).pubsub()
    subscription.subscribe(bus.channel)
    confirmation = subscription.get_message(timeout=2.0)
    assert confirmation is not None and confirmation["type"] == "subscribe"
    stop = threading.Event()

    def watch() -> None:
        while not stop.is_set():
            message = subscription.get_message(timeout=0.05)
            if message is not None and message["type"] == "message":
                seen.append((json.loads(str(message["data"])), snapshot()))

    watcher = threading.Thread(target=watch)
    watcher.start()
    try:
        yield seen
        time.sleep(0.3)
    finally:
        stop.set()
        watcher.join()
        subscription.close()
        client.close()

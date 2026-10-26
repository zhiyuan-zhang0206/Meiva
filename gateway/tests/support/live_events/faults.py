# meiva:note
# A live-events bus whose every publish fails with an error the publisher does not absorb.

from __future__ import annotations

from base.clock.tests.seeded_rng import seeded_rng
from base.events.live.bus import EventBus, EventBusConfig

__all__ = ["unpublishable_bus"]


# meiva:note
# A bus pointing at a malformed Redis URL: connecting raises `ValueError`.
#
# Known network failures are absorbed by the best-effort publishers; a malformed URL is not one
# of them, so a request that publishes through this bus fails after its durable writes, the way
# a process lost between commit and announce does.
def unpublishable_bus() -> EventBus:
    return EventBus(
        EventBusConfig(redis_url="redis://[unpublishable", events_channel="unpublished"),
        rng=seeded_rng(),
    )

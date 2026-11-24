from __future__ import annotations

from collections.abc import Callable

from base.clock import SYSTEM_TIMERS
from base.clock.tests.seeded_rng import seeded_rng
from base.events.live.bus import EventBus, event_bus_config_from_settings
from base.events.live.tests.fakes import sync_redis_factory


def bus_with_sync_redis(make_client: Callable[[], object]) -> EventBus:
    return EventBus(
        event_bus_config_from_settings(),
        rng=seeded_rng(),
        timers=SYSTEM_TIMERS,
        open_sync_redis=sync_redis_factory(make_client),
    )

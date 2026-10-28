from __future__ import annotations

import sys

from base.agents.context import MeivaContext, deferred_event_bus
from base.clock import SYSTEM_TIMERS
from base.clock.tests.seeded_rng import seeded_rng
from base.config import ConfigBoot

context = MeivaContext(
    bus_factory=deferred_event_bus(ConfigBoot(), rng=seeded_rng(), timers=SYSTEM_TIMERS)
)
loaded = sorted(m for m in ("redis", "base.events.live.bus") if m in sys.modules)
sys.stdout.write(f"{loaded}\n")

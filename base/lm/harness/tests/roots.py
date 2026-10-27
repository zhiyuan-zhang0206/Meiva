# meiva:note
# The values a host root hands a harness bridge, as a test builds them: a real clock over the system
# timers, the secure generator the session handle is drawn from, and this process's pid.

import os
import random

from base.clock.tests.fakes import system_clock
from base.lm.harness.registration import SecureRandom
from base.lm.harness.wiring import BridgeRootValues

__all__ = ["bridge_root_values"]


def bridge_root_values() -> BridgeRootValues:
    return BridgeRootValues(
        clock=system_clock(),
        controller_pid=os.getpid(),
        handle_rng=SecureRandom(random.SystemRandom()),
    )

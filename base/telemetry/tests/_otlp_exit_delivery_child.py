# meiva:note
# Short-lived child process for test_telemetry_otlp_exit_delivery.

from __future__ import annotations

import random
import sys
import time

from base import telemetry
from base.clock.tests.fakes import system_clock
from base.cluster.machine.configured import ConfiguredMachineIdentity
from base.config import ConfigBoot
from base.paths import meiva_home


def main() -> None:
    marker = sys.argv[1]
    config = ConfigBoot()
    config.read_process_environment()
    pipeline = telemetry.build_pipeline(
        now=system_clock().now,
        machine=ConfiguredMachineIdentity(config),
        home=meiva_home(),
        otlp_enabled=lambda: config.view.observability.telemetry_otlp_enabled,
        otlp_endpoint=lambda: config.view.observability.telemetry_otlp_endpoint,
        rng=random.SystemRandom(),
    )
    telemetry.init_telemetry(home=meiva_home(), process="exit-seam-probe", pipeline=pipeline)
    telemetry.emit("log", "log", attributes={"seq": "early", "marker": marker})
    telemetry.sync()
    if len(sys.argv) > 2:
        ready = meiva_home() / "first-batch"
        for _ in range(512):
            telemetry.emit("log", "log", attributes={"seq": "bulk", "marker": marker})
        for _ in range(400):
            if ready.exists():
                break
            time.sleep(0.02)
        else:
            raise RuntimeError("receiver never saw the first batch")
    for _ in range(3 if len(sys.argv) > 2 else 1):
        telemetry.emit("log", "log", attributes={"seq": "tail", "marker": marker})


if __name__ == "__main__":
    main()

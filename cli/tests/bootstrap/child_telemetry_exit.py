"""Child that dispatches one CLI verb and lets the exit drain deliver its telemetry.

Usage: `child_telemetry_exit.py <success|exception|empty|unavailable>`. Receiver, exporter and
OTel providers are all real.

The command's event pipeline holds a partial batch for `FLUSH_INTERVAL_S`, far longer than
the handler runs, and the handler waits `HOLD_S` (several default flush intervals) after it
emits. The child prints the wall-clock instant the command returned: the parent requires every
OTLP request to have arrived after it, which proves the first batch was written by the drain
that follows the command, not by the timer.
"""

import argparse
import sys
import time
from pathlib import Path

from base import telemetry
from base.cluster.machine import MachineIdentityReader
from base.paths.home import resolve_meiva_home
from cli import main
from cli.database import OperatorEventPipeline, open_operator_database
from cli.database.runtime import CommandRuntime

FLUSH_INTERVAL_S = 60
HOLD_S = 1.5

returned_at: list[float] = []


def dispatch(args: argparse.Namespace) -> int:
    try:
        if sys.argv[1] != "empty":
            telemetry.init_telemetry(
                home=resolve_meiva_home(), process="cli-health-probe", pipeline=args.producer()
            )
            telemetry.emit("telemetry", "health_probe_ran", attributes={"unhealthy_checks": 0})
            time.sleep(HOLD_S)
        if sys.argv[1] == "exception":
            raise RuntimeError("handler failure")
        return 7
    finally:
        returned_at.append(time.time())


def _producer(
    machine: MachineIdentityReader, home: Path, command: CommandRuntime
) -> OperatorEventPipeline:
    return OperatorEventPipeline(
        machine=machine,
        home=home,
        now=command.clock.now,
        otlp_enabled=command.otlp_enabled,
        otlp_endpoint=command.otlp_endpoint,
        rng=command.rng,
        flush_interval_s=FLUSH_INTERVAL_S,
    )


try:
    result = main.dispatch_parsed(
        argparse.Namespace(func=dispatch),
        ["status"],
        declared=resolve_meiva_home(),
        checkout=Path(__file__).resolve().parents[3],
        producer_factory=_producer,
        open_database=open_operator_database,
    )
except RuntimeError as exc:
    assert str(exc) == "handler failure"
else:
    assert result == 7
print(f"command_returned_at={returned_at[0]!r}")

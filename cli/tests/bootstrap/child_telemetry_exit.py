"""Child that dispatches one CLI verb and lets the exit drain deliver its telemetry.

Usage: `child_telemetry_exit.py <success|exception|empty|unavailable>`. Receiver and
OTel providers are real; only data persistence is isolated.

The command's event pipeline holds a partial batch for `FLUSH_INTERVAL_S`, far longer than
the handler runs, and the handler waits `HOLD_S` (several default flush intervals) after it
emits. A batch written inside that window can only come from a timer, so the child prints how
many there were: the parent requires zero, which proves the first batch was written by the
drain that follows the command, not by the timer.
"""

import argparse
import sys
import threading
import time
from pathlib import Path
from typing import Any

from base import telemetry
from base.cluster.machine import MachineIdentityReader
from base.paths.home import resolve_meiva_home
from base.telemetry import event_store
from cli import main
from cli.database import OperatorDatabaseFactory, OperatorEventPipeline, open_operator_database

FLUSH_INTERVAL_S = 60
HOLD_S = 1.5

command_returned = threading.Event()
early_writes: list[float] = []


class WatchedDatabase(OperatorDatabaseFactory):
    """The database handle the writer asks for before every batch it persists."""

    def __call__(self) -> Any:
        if not command_returned.is_set():
            early_writes.append(time.monotonic())
        return super().__call__()


event_store.set_enabled(enabled=False)


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
        command_returned.set()


def _producer(
    database_factory: OperatorDatabaseFactory, machine: MachineIdentityReader, home: Path
) -> OperatorEventPipeline:
    del database_factory
    return OperatorEventPipeline(
        WatchedDatabase(
            local_host=machine.host,
            home=home,
            open_database=open_operator_database,
        ),
        machine=machine,
        home=home,
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
print(f"early_batch_writes={len(early_writes)}")

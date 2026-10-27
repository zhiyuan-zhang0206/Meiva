"""Value contract for resources borrowed by an in-process schedule script."""

from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from base.cluster.machine import MachineIdentityReader
from base.db import Database
from base.native_process.loaded_commit import LoadedCommit
from base.telemetry import EventPipeline

__all__ = ["ScheduleInputs", "schedule_entry"]


# meiva:note
# Use the executing entry's builders, the home it resolved and its immutable loaded-code fact.
@dataclass(frozen=True)
class ScheduleInputs:
    database: Callable[[], Database]
    producer: Callable[[], EventPipeline]
    image: LoadedCommit
    machine: MachineIdentityReader = field(kw_only=True)
    home: Path = field(kw_only=True)


# meiva:note
# Borrow the executing schedule entry's builders and loaded-code fact.
@contextmanager
def schedule_entry(inputs: ScheduleInputs) -> Generator[ScheduleInputs]:
    yield inputs

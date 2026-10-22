"""Value contract for resources borrowed by an in-process schedule script."""

from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass, field

from base.cluster.machine import MachineIdentity
from base.db import Database
from base.native_process.loaded_commit import LoadedCommit
from base.telemetry import EventPipeline

__all__ = ["ScheduleInputs", "schedule_entry"]


@dataclass(frozen=True)
class ScheduleInputs:
    """Use the executing entry's builders and immutable loaded-code fact."""

    database: Callable[[], Database]
    producer: Callable[[], EventPipeline]
    image: LoadedCommit
    machine: MachineIdentity = field(kw_only=True)


# meiva:note
# Borrow the executing schedule entry's builders and loaded-code fact.
@contextmanager
def schedule_entry(inputs: ScheduleInputs) -> Generator[ScheduleInputs]:
    yield inputs

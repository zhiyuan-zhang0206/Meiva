# meiva:note
# Emit one declared telemetry event from a schedule that otherwise runs quietly.
#
# A schedule that fires once a day opens process logging and the event writer only when it has
# something to report: `init_gateway_process` registers the process's log file and event sink
# (and a `service_started` row) once, and then the event is emitted through that pipeline.
# A quiet exit never builds a writer. The `producer` is the entry's lazy event pipeline.
# meiva:link schedules/README.md

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path

from base import telemetry
from base.native_process.loaded_commit import LoadedCommit
from base.telemetry import EventPipeline
from base.telemetry.process_logging import init_gateway_process

__all__ = ["emit_system_event"]


def emit_system_event(
    event_name: str,
    attributes: Mapping[str, object],
    *,
    process: str,
    producer: Callable[[], EventPipeline],
    machine_reader: Callable[[], str],
    image: LoadedCommit,
    home: Path,
) -> None:
    init_gateway_process(
        name=process, producer=producer, machine_reader=machine_reader, image=image, home=home
    )
    telemetry.emit("telemetry", event_name, source="system", attributes=dict(attributes))

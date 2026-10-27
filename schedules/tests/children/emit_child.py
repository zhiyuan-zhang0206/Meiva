# meiva:note
# Child process for the telemetry-event test: a fresh interpreter that emits one declared event
# through `emit_system_event` and reports the log files and events it produced as JSON.
#
# Process logging is process-global state, so the test runs it here instead of in pytest. The
# event writer is an in-memory list; the machine is named "test-host".

from __future__ import annotations

import json
import sys
from pathlib import Path

from base.log import logger, prepare_process_logging
from base.native_process.loaded_commit import LoadedCommit
from base.paths import logs_dir
from base.telemetry.delivery.pipeline import EventPipeline
from base.telemetry.delivery.receipts import Event
from schedules.jobs.telemetry_event import emit_system_event


def main(process: str, home: Path) -> None:
    prepare_process_logging()
    events: list[Event] = []
    pipeline = EventPipeline(writer=events.extend)
    try:
        for _ in range(2):
            emit_system_event(
                "ci_usage_daily",
                {"runs": 3, "appended_runs": 1},
                process=process,
                producer=lambda: pipeline,
                machine_reader=lambda: "test-host",
                image=LoadedCommit(Path.cwd(), "test-image"),
                home=home,
            )
    finally:
        pipeline.stop(timeout=2)
        logger.remove()
    sys.stdout.write(
        json.dumps(
            {
                "files": sorted(path.name for path in logs_dir(home=home).glob("*.log")),
                "events": [
                    {"name": event.event_name, "attributes": event.attributes} for event in events
                ],
            }
        )
    )


if __name__ == "__main__":
    main(sys.argv[1], Path(sys.argv[2]))

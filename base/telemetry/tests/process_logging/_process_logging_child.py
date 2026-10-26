# meiva:note
# Exercise process-owned logging state in a fresh interpreter.

from __future__ import annotations

import json
import sys
import threading
from functools import partial
from pathlib import Path

from base.log import logger, prepare_process_logging
from base.log.process import init_subprocess_logger
from base.native_process.loaded_commit import LoadedCommit
from base.paths import logs_dir
from base.telemetry.delivery.pipeline import EventPipeline
from base.telemetry.delivery.receipts import Event
from base.telemetry.process_logging import init_cli_process, init_gateway_process


def main(role: str, home: Path | None) -> None:
    prepare_process_logging()
    events: list[Event] = []
    pipeline = EventPipeline(writer=events.extend)
    before = set(threading.enumerate())
    try:
        for _ in range(3):
            if role == "subprocess":
                init_subprocess_logger(agent_id=17, home=home)
            elif role == "cli":
                init_cli_process(
                    name="cli-watchdog-update",
                    producer=lambda: pipeline,
                    machine_reader=lambda: "test-host",
                    home=home,
                )
            else:
                initialize = partial(
                    init_gateway_process,
                    producer=lambda: pipeline,
                    machine_reader=lambda: "test-host",
                    image=LoadedCommit(Path.cwd(), "test-image"),
                    home=home,
                )
                if role == "default-gateway":
                    initialize()
                else:
                    initialize("restarter")
                if role == "cross-role":
                    init_subprocess_logger(agent_id=17, home=home)
        logger.warning("idempotency-probe")
        new_threads = [thread.name for thread in threading.enumerate() if thread not in before]
    finally:
        pipeline.stop(timeout=2)
        logger.remove()
    sys.stdout.write(
        json.dumps(
            {
                "new_threads": new_threads,
                "files": {
                    path.name: [
                        json.loads(line)["record"] for line in path.read_text().splitlines()
                    ]
                    for path in logs_dir(home=home).glob("*.log")
                },
                "events": [
                    {"name": event.event_name, "attributes": event.attributes} for event in events
                ],
            }
        )
    )


if __name__ == "__main__":
    # meiva:note
    # An optional second argument is the home the entry hands in; without it the child's ambient
    # home is used.
    main(sys.argv[1], Path(sys.argv[2]) if len(sys.argv) > 2 else None)

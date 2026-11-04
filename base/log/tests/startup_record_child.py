# meiva:note
# Child process for the startup-record test: buffer one record, then start the process sinks, so
# the test observes the composition (`start_process_logging` replaying the buffer) in a process
# whose module-level idempotency guard has not been used yet -- without patching that guard.

from __future__ import annotations

import logging
import sys
from pathlib import Path

from base.log import process


def main(home: str) -> None:
    logging.basicConfig(level=logging.INFO)
    process.buffer_startup_records()
    logging.getLogger("services.supervision.meiva_root.supervisor").info("unit svc started (pid 7)")
    process.start_process_logging("meiva-root", attach_events=lambda: None, home=Path(home))


if __name__ == "__main__":
    main(sys.argv[1])

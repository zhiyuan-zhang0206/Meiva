# meiva:note
# Child process for the "opens its own sink" half of the redaction test: no `init_*`, no
# `prepare_process_logging` -- exactly the shape of `exec_host`, `pty_sessions` and the MCP daemon,
# which add their own stderr sink and would otherwise log every record unmasked.

from __future__ import annotations

import sys
from pathlib import Path

from base.log import logger
from base.log.sinks import add_sink


def main(target: str) -> None:
    add_sink(Path(target), format="{message}", level="INFO")
    logger.info("export MEIVA_API_TOKEN=fixture-value-sink")
    logger.info("dial postgresql://runner:fixture-value-sink@10.1.2.3:6433/db")


if __name__ == "__main__":
    main(sys.argv[1])

# meiva:note
# A real refused start must open neither process sinks nor its lazy telemetry writer.

import json
import sys
from pathlib import Path

SINK_SENTINEL = "start-gate-no-sink-sentinel"


def main(home: Path, checkout: Path) -> None:
    from base.log import prepare_process_logging

    prepare_process_logging()
    from base.log import logger, process
    from cli import start_intent
    from cli.database import (
        open_operator_database,
        operator_database_factory,
        operator_event_pipeline,
    )
    from cli.database.runtime import CommandRuntime
    from cli.parsers import build_parser
    from cli.start_runtime import HomeSite

    assert not process.init_done, "the refusal starts before any process logger initialization"
    command = CommandRuntime.create(home)
    database = operator_database_factory(
        local_host=command.machine.host, home=command.home, open_database=open_operator_database
    )
    producer = operator_event_pipeline(database, machine=command.machine, home=home)
    args = build_parser().parse_args(["start", "--all-services"])
    args.runtime = command
    try:
        code = start_intent.run_start(
            args,
            HomeSite(home, checkout),
            retained_children=[],
            database_factory=database,
            producer=producer,
        )
    finally:
        drained = producer.close(timeout=1.0)
        logger.error(SINK_SENTINEL)
    assert drained is None, "a refused start must not construct its telemetry writer"
    assert not process.init_done
    assert not command.config.prepared
    print(json.dumps({"code": code}))


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]))

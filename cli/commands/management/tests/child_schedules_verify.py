import sys

from base.paths import meiva_home
from cli.commands.management.schedules_verify import cmd_schedules_verify
from cli.database import (
    open_operator_database,
    operator_database_factory,
    operator_event_pipeline,
)
from cli.database.runtime import CommandRuntime


def _check_rows(rows_file: str) -> None:
    command = CommandRuntime.create()
    database = operator_database_factory(
        local_host=command.machine.host,
        open_database=open_operator_database,
    )
    producer = operator_event_pipeline(database, machine=command.machine, home=meiva_home())
    try:
        sys.exit(cmd_schedules_verify(notify=False, rows_file=rows_file, producer=producer))
    finally:
        producer.close()


if __name__ == "__main__":
    if sys.argv[1] == "check_rows":
        _check_rows(sys.argv[2])
    else:
        raise SystemExit(f"unknown scenario {sys.argv[1]!r}")

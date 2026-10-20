import inspect
import json
import sys
from pathlib import Path
from typing import Any

from cli.commands.management import schedules_verify as verify


def read_rows(out: str) -> None:
    rows: list[tuple[int, str, str]]
    read_schedule_rows: Any = verify._read_schedule_rows
    if "database_factory" in inspect.signature(read_schedule_rows).parameters:
        from cli.database import operator_database_factory
        from cli.database.runtime import CommandRuntime

        command = CommandRuntime.create()
        parameters = inspect.signature(operator_database_factory).parameters
        if "gateway_url_reader" in parameters:
            from cli.database import open_operator_database

            gateway_factory: Any = operator_database_factory
            database_factory = gateway_factory(
                local_host=command.machine.host,
                gateway_url_reader=lambda: command.config.view.gateway.gateway_url,
                open_database=open_operator_database,
            )
        elif "open_database" in parameters:
            from cli.database import open_operator_database

            legacy_factory: Any = operator_database_factory
            database_factory = legacy_factory(
                local_host=command.machine.host, open_database=open_operator_database
            )
        else:
            legacy_factory = operator_database_factory
            database_factory = legacy_factory(local_host=command.machine.host)
        rows = read_schedule_rows(database_factory=database_factory)
    elif "access" in inspect.signature(read_schedule_rows).parameters:
        from base.cluster.machine.boot import gateway_access_from_boot
        from cli.database.runtime import CommandRuntime

        rows = read_schedule_rows(gateway_access_from_boot(CommandRuntime.create().config))
    else:
        rows = read_schedule_rows()
    with Path(out).open("w") as handle:
        json.dump([list(row) for row in rows], handle)


if __name__ == "__main__":
    read_rows(sys.argv[1])

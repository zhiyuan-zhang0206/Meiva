import inspect
import json
import sys
from pathlib import Path
from typing import Any

from cli.commands.management import schedules_verify as verify

_LEGACY_READ = "_read_schedule_rows"


def read_rows(out: str) -> None:
    rows: list[tuple[int, str, str]]
    read_schedule_rows: Any = getattr(verify, "read_schedule_rows", None) or getattr(
        verify, _LEGACY_READ
    )
    if "database_factory" in inspect.signature(read_schedule_rows).parameters:
        from cli.database import operator_database_factory

        command = _command_runtime()
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

        rows = read_schedule_rows(gateway_access_from_boot(_command_runtime().config))
    else:
        rows = read_schedule_rows()
    with Path(out).open("w") as handle:
        json.dump([list(row) for row in rows], handle)


# meiva:note
# The invocation's runtime under whichever checkout this program runs in. This file is shipped
# to hosts whose installed code predates the checkout it came from, and `CommandRuntime.create`
# takes the resolved home only in newer ones.
def _command_runtime() -> Any:
    from cli.database.runtime import CommandRuntime

    create: Any = CommandRuntime.create
    if "home" in inspect.signature(create).parameters:
        from base.paths.home import resolve_meiva_home

        return create(resolve_meiva_home().resolve())
    return create()


if __name__ == "__main__":
    read_rows(sys.argv[1])

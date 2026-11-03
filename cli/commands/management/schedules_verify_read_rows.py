from __future__ import annotations

import inspect
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TypeGuard

from cli.commands.management import schedules_verify as verify

_LEGACY_READ = "_read_schedule_rows"

type _Rows = list[tuple[int, str, str]]


def _is_list(value: object) -> TypeGuard[list[object]]:
    return isinstance(value, list)


def _is_row(value: object) -> TypeGuard[tuple[object, ...] | list[object]]:
    return isinstance(value, tuple | list)


# meiva:note
# Call `function` with `keywords`. The functions passed here are looked up by name and signature in
# whichever checkout this file runs in, so their keywords are checked at run time, not here.
def _call(function: Callable[..., object], /, **keywords: object) -> object:
    return function(**keywords)


# meiva:note
# The `(id, name, script)` rows a generation's reader returned, checked at this edge: the reader is
# found by name in whichever checkout this file is shipped to, so what it returns is not typed here.
def _rows(value: object) -> _Rows:
    if not _is_list(value):
        raise TypeError(f"schedule rows are {type(value).__name__}, not a list")
    rows: _Rows = []
    for row in value:
        if not _is_row(row) or len(row) != 3 or not isinstance(row[0], int):
            raise TypeError("a schedule row is not an (id, name, script) triple")
        rows.append((row[0], str(row[1]), str(row[2])))
    return rows


# meiva:note
# `function(*args)` as an `object`, so a call the standard library types as `Any` is read as
# `object`. The file is shipped to hosts whose installed code predates `cli.boundary`, so it keeps
# its own copy of `cli.boundary.untyped.object_of`.
def _object_of[**P](function: Callable[P, object], /, *args: P.args, **kwargs: P.kwargs) -> object:
    return function(*args, **kwargs)


def _current_reader() -> object:
    return _object_of(getattr, verify, "read_schedule_rows", None)


def _legacy_reader() -> object:
    return _object_of(getattr, verify, _LEGACY_READ, None)


# meiva:note
# The schedule reader this checkout's `schedules_verify` has: `read_schedule_rows`, or the name an
# older generation gave it.
def _reader() -> Callable[..., object]:
    found = _current_reader() or _legacy_reader()
    if found is None:
        raise AttributeError(f"module has neither read_schedule_rows nor {_LEGACY_READ}")
    if not callable(found):
        raise TypeError("the schedule reader is not callable")
    return found


def read_rows(out: str) -> None:
    read_schedule_rows = _reader()
    parameters = inspect.signature(read_schedule_rows).parameters
    if "database_factory" in parameters:
        from cli.database import operator_database_factory
        from cli.database.runtime import CommandRuntime

        command = _runtime(CommandRuntime.create)
        factory_parameters = inspect.signature(operator_database_factory).parameters
        if "home" in factory_parameters:
            from cli.database import open_operator_database

            database_factory = _call(
                operator_database_factory,
                local_host=command.machine.host,
                home=command.home,
                open_database=open_operator_database,
            )
        elif "gateway_url_reader" in factory_parameters:
            from cli.database import open_operator_database

            database_factory = _call(
                operator_database_factory,
                local_host=command.machine.host,
                gateway_url_reader=lambda: command.config.view.gateway.gateway_url,
                open_database=open_operator_database,
            )
        elif "open_database" in factory_parameters:
            from cli.database import open_operator_database

            database_factory = _call(
                operator_database_factory,
                local_host=command.machine.host,
                open_database=open_operator_database,
            )
        else:
            database_factory = _call(operator_database_factory, local_host=command.machine.host)
        rows = _rows(_call(read_schedule_rows, database_factory=database_factory))
    elif "access" in parameters:
        from base.cluster.machine.boot import gateway_access_from_boot
        from cli.database.runtime import CommandRuntime

        command = _runtime(CommandRuntime.create)
        rows = _rows(read_schedule_rows(gateway_access_from_boot(command.config)))
    else:
        rows = _rows(read_schedule_rows())
    with Path(out).open("w") as handle:
        json.dump([list(row) for row in rows], handle)


# meiva:note
# The invocation's runtime under whichever checkout this program runs in. This file is shipped
# to hosts whose installed code predates the checkout it came from, and `CommandRuntime.create`
# takes the resolved home only in newer ones: `create` is asked for its signature here instead of
# being called with the arguments the checkout this was written against takes.
def _runtime[R](create: Callable[..., R]) -> R:
    if "home" in inspect.signature(create).parameters:
        from base.paths.home import resolve_meiva_home

        return create(resolve_meiva_home().resolve())
    return create()


if __name__ == "__main__":
    read_rows(sys.argv[1])

# meiva:note
# `cli.fleet_update` pipes this file's source to `python -` on the gateway host, like its roster
# and silence programs, so the first update carrying this change already has it. It reports the
# takeovers a stop would abort; every failure prints one line and exits 0, because an unreadable
# lease table must warn, never strand an update.
# meiva:link cli/docs/fleet-update-window/fleet-update-window.meiva.okf.md
from __future__ import annotations

import inspect
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

import psycopg
from psycopg.rows import TupleRow, class_row

_QUERY = (
    "SELECT agent_id, session_id, executor_name, expires_at FROM agent_impersonations "
    "WHERE status='active' ORDER BY expires_at, agent_id"
)


@dataclass(frozen=True)
class _LeaseRow:
    agent_id: int
    session_id: int
    executor_name: str
    expires_at: datetime


# meiva:note
# Every active lease, one row per takeover: the session the operator acts on, and none of the
# token hashes, the handoff document or the process metadata the log must not carry. The filter is
# the status the table holds rather than the clock -- a stopped cluster's reaper lags, and a row
# still marked active is exactly the one a stop would end.
def active_leases(conn: psycopg.Connection[TupleRow]) -> list[dict[str, str | int]]:
    with conn.cursor(row_factory=class_row(_LeaseRow)) as cursor:
        rows = cursor.execute(_QUERY).fetchall()
    return [
        {
            "agent_id": row.agent_id,
            "session_id": row.session_id,
            "executor": row.executor_name,
            "expires_at": row.expires_at.isoformat(timespec="seconds"),
        }
        for row in rows
    ]


# meiva:note
# `function` as a callable of any arguments: which generation's signature it has is decided by
# `inspect.signature` where it is called. This program is piped to hosts whose installed code
# predates the checkout it came from.
def _by_signature[T](function: Callable[..., T]) -> Callable[..., T]:
    return function


# meiva:note
# A connection through this home's own operator handle, the one every `meiva` command reads
# through: the program needs nothing from the operator's checkout but it.
def operator_connection() -> psycopg.Connection[TupleRow]:
    from cli.database import open_operator_database, operator_database_factory
    from cli.database.runtime import CommandRuntime

    # meiva:note
    # `CommandRuntime.create` and the operator factory take the resolved home only in newer
    # generations.
    create = _by_signature(CommandRuntime.create)
    if "home" in inspect.signature(create).parameters:
        from base.paths.home import resolve_meiva_home

        command = create(resolve_meiva_home().resolve())
    else:
        command = create()
    build = _by_signature(operator_database_factory)
    if "home" in inspect.signature(build).parameters:
        factory = build(
            local_host=command.machine.host, home=command.home, open_database=open_operator_database
        )
    else:
        factory = build(local_host=command.machine.host, open_database=open_operator_database)
    return factory().connect(autocommit=True)


# meiva:note
# One JSON line for the operator's console; a failure is reported here with exit 0 so the caller
# warns and goes on instead of raising.
def main() -> int:
    try:
        with operator_connection() as conn:
            leases = active_leases(conn)
    except Exception as exc:
        print(f"LEASES failed: {type(exc).__name__}: {exc}")
        return 0
    print(json.dumps({"leases": leases}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

# meiva:note
# `cli.fleet_update` pipes this file's source to `python -` on the gateway host, like its roster
# and silence programs, so the first update carrying this change already has it. It reports the
# takeovers a stop would abort; every failure prints one line and exits 0, because an unreadable
# lease table must warn, never strand an update.
# meiva:link cli/docs/fleet-update-window.meiva.okf.md
from __future__ import annotations

import json
from typing import Any

_QUERY = (
    "SELECT agent_id, session_id, executor_name, expires_at FROM agent_impersonations "
    "WHERE status='active' ORDER BY expires_at, agent_id"
)


# meiva:note
# Every active lease, one row per takeover: the session the operator acts on, and none of the
# token hashes, the handoff document or the process metadata the log must not carry. The filter is
# the status the table holds rather than the clock -- a stopped cluster's reaper lags, and a row
# still marked active is exactly the one a stop would end.
def active_leases(database: Any) -> list[dict[str, Any]]:
    with database.connect(autocommit=True) as conn:
        rows = conn.execute(_QUERY).fetchall()
    return [
        {
            "agent_id": int(agent_id),
            "session_id": int(session_id),
            "executor": str(executor),
            "expires_at": expires_at.isoformat(timespec="seconds"),
        }
        for agent_id, session_id, executor, expires_at in rows
    ]


# meiva:note
# This home's own operator handle, the one every `meiva` command reads through: the program needs
# nothing from the operator's checkout but it.
def operator_database() -> Any:
    from cli.database import open_operator_database, operator_database_factory
    from cli.database.runtime import CommandRuntime

    command = CommandRuntime.create()
    factory = operator_database_factory(
        local_host=command.machine.host, open_database=open_operator_database
    )
    return factory()


# meiva:note
# One JSON line for the operator's console; a failure is reported here with exit 0 so the caller
# warns and goes on instead of raising.
def main() -> int:
    try:
        leases = active_leases(operator_database())
    except Exception as exc:
        print(f"LEASES failed: {type(exc).__name__}: {exc}")
        return 0
    print(json.dumps({"leases": leases}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

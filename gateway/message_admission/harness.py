# meiva:note
# Stored harness lookup shared by chat admission and timeline readers.

from dataclasses import dataclass

import psycopg

from base.db.rows import query_one


# meiva:note
# The harness an agent runs on; "meiva" when the column is NULL (a row born before it existed).
@dataclass(frozen=True)
class _HarnessRow:
    harness: str | None


def agent_harness(conn: psycopg.Connection, agent_id: int) -> str:
    row = query_one(conn, _HarnessRow, "SELECT harness FROM agents_meta WHERE id = %s", (agent_id,))
    return row.harness if row is not None and row.harness is not None else "meiva"

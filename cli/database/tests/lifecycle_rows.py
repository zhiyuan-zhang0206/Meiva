# meiva:note
# The lifecycle audit of a command under test, read back from the real database.
#
# `lifecycle_rows` opens the audit scope an operator command opens, with the recorders that
# write through the suite's operator database, so what a command asks the root for lands as
# `audit_events` rows. A test asserts on those rows (their verb, units and the time they were
# written), not on a recorder it handed in itself.

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime

import psycopg
import pytest

from base.native_process.root_control.ipc import CallerPayload
from cli.database import OperatorDatabaseFactory
from cli.database.lifecycle_audit import (
    lifecycle_audit,
    operator_request_recorder,
    operator_result_recorder,
)

__all__ = ["LifecycleRows", "caller_block", "lifecycle_rows"]


# meiva:note
# The caller block a remote operator's command carries on the wire; `agent_id` None is a human.
def caller_block(operation: str, *, agent_id: str | None = "1818") -> CallerPayload:
    return {
        "pid": 4821,
        "ppid": 4800,
        "uid": 501,
        "cwd": "/src",
        "argv": ["meiva", "stop", "-y", "--token", "<redacted>"],
        "agent_id": agent_id,
        "machine": "macmini",
        "operation_id": operation,
        "env_keys": ["MEIVA_API_TOKEN"],
    }


# meiva:note
# The lifecycle requests one operation wrote, oldest first.
@dataclass(frozen=True)
class LifecycleRows:
    connection: psycopg.Connection
    operation: str

    # meiva:note
    # Each request as `(verb, units)`, in the order it was written.
    def requests(self) -> list[tuple[str, list[str]]]:
        rows = self.connection.execute(
            "SELECT attributes->>'verb', attributes->'units' FROM audit_events "
            "WHERE event_name = 'lifecycle_request' AND attributes->>'operation_id' = %s "
            "ORDER BY id",
            (self.operation,),
        ).fetchall()
        return [(str(verb), list(units)) for verb, units in rows]

    # meiva:note
    # The database's clock at the moment each request row was written, in the order written.
    def written_at(self) -> list[datetime]:
        rows = self.connection.execute(
            "SELECT recorded_at FROM audit_events "
            "WHERE event_name = 'lifecycle_request' AND attributes->>'operation_id' = %s "
            "ORDER BY id",
            (self.operation,),
        ).fetchall()
        return [moment for (moment,) in rows]


@pytest.fixture
def lifecycle_rows(
    operator_database: OperatorDatabaseFactory, db_conn: psycopg.Connection
) -> Iterator[LifecycleRows]:
    """Run the test inside an operator command's audit scope; read its requests back from Postgres."""
    operation = f"test@{uuid.uuid4().hex}"
    caller = caller_block(operation)
    with lifecycle_audit(
        operator_request_recorder(database_factory=operator_database, caller=caller, head_sha=None),
        operator_result_recorder(database_factory=operator_database, caller=caller, head_sha=None),
    ):
        yield LifecycleRows(db_conn, operation)

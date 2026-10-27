# meiva:note
# The open-tasks hint is advisory: a failed read never changes an accepted termination.
#
# The home runner is a real listener (`gateway/tests/support/runner_ops`). The read fails for real: the
# table is missing, or the pool has no connection left for the read and times out.

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from base.db import Database, create_agent
from gateway.agents import lifecycle
from gateway.tests.auth.gateway_config import gateway_test_config, gateway_test_rpc
from gateway.tests.support.db_faults.missing_table import missing_table
from gateway.tests.support.runner_ops.ops_stub import (
    OpsCall,
    OpsReply,
    completed,
    expecting,
    runner_ops,
)
from ops.rpc_schemas import TerminateAgentRequest

_ACCEPTED = {"status": "enqueued", "open_tasks": None, "shell_sessions": None}


def _agent_on(db_conn: psycopg.Connection, machine: str) -> int:
    agent_id = create_agent(db_conn)
    db_conn.execute(
        "INSERT INTO agents_meta(id, status, machine) VALUES (%s, 'running', %s)",
        (agent_id, machine),
    )
    db_conn.commit()
    return agent_id


@contextmanager
def _pool_with_one_connection(database: Database) -> Generator[ConnectionPool]:
    with database.pool(min_size=1, max_size=1, timeout=0.3) as pool:
        yield pool


@pytest.mark.asyncio
async def test_a_missing_task_table_never_changes_an_accepted_termination(
    database: Database, db_conn: psycopg.Connection
) -> None:
    agent_id = _agent_on(db_conn, "hint-runner")
    forwarded = {
        "path": f"/api/agents/{agent_id}/terminate",
        "body": TerminateAgentRequest().model_dump(),
    }
    with (
        runner_ops(
            db_conn,
            "hint-runner",
            {"lifecycle": expecting(forwarded, completed({"status": "enqueued"}))},
        ),
        database.pool(max_size=2) as pool,
        missing_table(db_conn, "agent_tasks"),
    ):
        response = await lifecycle.terminate_agent_with_open_tasks(
            agent_id,
            TerminateAgentRequest(),
            pool,
            db=database,
            config=gateway_test_config(),
            rpc=gateway_test_rpc(),
        )
    assert response.model_dump() == _ACCEPTED


@pytest.mark.asyncio
async def test_a_pool_with_no_connection_left_never_changes_an_accepted_termination(
    database: Database, db_conn: psycopg.Connection
) -> None:
    agent_id = _agent_on(db_conn, "hint-runner")
    held: list[psycopg.Connection] = []
    with _pool_with_one_connection(database) as pool:

        def accept_and_keep_the_connection(call: OpsCall) -> OpsReply:
            held.append(pool.getconn())
            return completed({"status": "enqueued"})

        with runner_ops(db_conn, "hint-runner", {"lifecycle": accept_and_keep_the_connection}):
            response = await lifecycle.terminate_agent_with_open_tasks(
                agent_id,
                TerminateAgentRequest(),
                pool,
                db=database,
                config=gateway_test_config(),
                rpc=gateway_test_rpc(),
            )
        for conn in held:
            pool.putconn(conn)
    assert len(held) == 1
    assert response.model_dump() == _ACCEPTED

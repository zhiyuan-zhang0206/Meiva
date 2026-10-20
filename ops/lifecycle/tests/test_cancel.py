# meiva:note
# `cancel_agent_op`: the stop request reaches the home host and reports what it did.

from __future__ import annotations

import httpx
import psycopg
import pytest

from base.api_contracts.agent_contract import AgentNotFound, CancelResult
from base.db import Database, create_agent
from ops.lifecycle import cancel_agent_op


def _agent(db_conn: psycopg.Connection) -> int:
    agent_id = create_agent(db_conn)
    db_conn.execute("INSERT INTO agents_meta (id,status) VALUES (%s,'running')", (agent_id,))
    db_conn.commit()
    return agent_id


@pytest.mark.parametrize(
    ("cancelled", "expected"),
    [(True, CancelResult.CANCELLED), (False, CancelResult.NOT_RUNNING)],
)
async def test_reports_what_the_host_answered(
    db_conn: psycopg.Connection, database: Database, cancelled: bool, expected: CancelResult
) -> None:
    agent_id = _agent(db_conn)
    asked: list[int] = []

    async def cancel(requested: int) -> bool:
        asked.append(requested)
        return cancelled

    response = await cancel_agent_op(database, agent_id, cancel=cancel)
    assert response.status is expected
    assert asked == [agent_id]


async def test_an_unknown_agent_is_never_cancelled(database: Database) -> None:
    async def cancel(_agent_id: int) -> bool:
        raise AssertionError("the host must not be asked about an unknown agent")

    with pytest.raises(AgentNotFound):
        await cancel_agent_op(database, 987654, cancel=cancel)


async def test_an_unreachable_host_is_an_error_not_a_quiet_not_running(
    db_conn: psycopg.Connection, database: Database
) -> None:
    agent_id = _agent(db_conn)

    async def cancel(_agent_id: int) -> bool:
        raise httpx.ConnectError("agent host is down")

    with pytest.raises(httpx.ConnectError):
        await cancel_agent_op(database, agent_id, cancel=cancel)

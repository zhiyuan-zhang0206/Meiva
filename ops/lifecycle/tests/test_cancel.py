# meiva:note
# `cancel_agent_op`: the stop request reaches the home host and reports what it did.

from __future__ import annotations

from pathlib import Path

import httpx
import psycopg
import pytest

from base.api_contracts.agent_contract import AgentNotFound, CancelResult
from base.db import Database, create_agent
from ops.lifecycle import cancel_agent_op
from ops.lifecycle.hosted_turn import LocalAgentHost
from ops.lifecycle.tests.loopback.agent_host import serve_agent_host
from ops.lifecycle.tests.loopback.scripted_post import Reply, closed_port, serve_post


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
    db_conn: psycopg.Connection,
    database: Database,
    unit_home: Path,
    cancelled: bool,
    expected: CancelResult,
) -> None:
    agent_id = _agent(db_conn)

    with serve_agent_host(unit_home, cancelled=cancelled) as served:
        response = await cancel_agent_op(database, agent_id, host=served.host)

    assert response.status is expected
    assert served.cancel_requests == [agent_id]


async def test_an_unknown_agent_is_never_cancelled(database: Database, unit_home: Path) -> None:
    with serve_agent_host(unit_home) as served, pytest.raises(AgentNotFound):
        await cancel_agent_op(database, 987654, host=served.host)

    assert served.cancel_requests == []


async def test_an_unreachable_host_is_an_error_not_a_quiet_not_running(
    db_conn: psycopg.Connection, database: Database, unit_home: Path
) -> None:
    agent_id = _agent(db_conn)
    host = LocalAgentHost(home=unit_home, health_port=closed_port())

    with pytest.raises(httpx.ConnectError):
        await cancel_agent_op(database, agent_id, host=host)


async def test_a_host_that_answers_with_an_error_status_is_an_error(
    db_conn: psycopg.Connection, database: Database, unit_home: Path
) -> None:
    agent_id = _agent(db_conn)

    with serve_post({"/cancel-turn": lambda _body: Reply({}, status=503)}) as served:
        host = LocalAgentHost(home=unit_home, health_port=served.port)
        with pytest.raises(httpx.HTTPStatusError):
            await cancel_agent_op(database, agent_id, host=host)

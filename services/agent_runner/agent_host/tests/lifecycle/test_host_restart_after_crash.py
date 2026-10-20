from typing import Any

import psycopg
from psycopg_pool import AsyncConnectionPool

from agent.ownership.hosted import settle_running_rows_at_start
from base.cluster.machine import MachineIdentity
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.db import create_agent


def _agent_on(conn: psycopg.Connection[Any], *, machine_name: str) -> int:
    agent_id = create_agent(conn)
    conn.execute(
        "INSERT INTO agents_meta(id,status,machine) VALUES(%s,'idling',%s)",
        (agent_id, machine_name),
    )
    conn.commit()
    return agent_id


async def test_boot_settles_only_this_machines_running_rows(
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
    machine_identity: MachineIdentity,
) -> None:
    local = machine_identity.name()
    running_here = _agent_on(db_conn, machine_name=local)
    running_elsewhere = _agent_on(db_conn, machine_name="another-machine")
    idling_here = _agent_on(db_conn, machine_name=local)
    terminated_here = _agent_on(db_conn, machine_name=local)
    db_conn.execute(
        "UPDATE agents_meta SET status='running' WHERE id IN (%s,%s)",
        (running_here, running_elsewhere),
    )
    db_conn.execute("UPDATE agents_meta SET status='terminated' WHERE id=%s", (terminated_here,))
    db_conn.commit()

    assert await settle_running_rows_at_start(aops_pool, local) == [running_here]

    rows = db_conn.execute(
        "SELECT id,status FROM agents_meta WHERE id IN (%s,%s,%s,%s) ORDER BY id",
        (running_here, running_elsewhere, idling_here, terminated_here),
    ).fetchall()
    db_conn.commit()
    assert dict(rows) == {
        running_here: "idling",
        running_elsewhere: "running",
        idling_here: "idling",
        terminated_here: "terminated",
    }

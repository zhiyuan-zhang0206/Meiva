# meiva:note
# An idle agent whose owner host has departed is parked by the restart cohort.
#
# A host that exits leaves its idle agents with the owner and the generation on the
# row. Nothing wakes such an agent until a message arrives, so after a host restart it
# stays in exactly that state. Only an agent with a running turn must have the live
# original owner; the departed-owner idle agent is parked and re-admitted by the
# ordinary admission path when it is next woken.

from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from agent.ownership.hosted import admit_hosted_runtime
from base.cluster.machine import MachineIdentity
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.db import Database
from base.deploy.maintenance import admission, cohort, pause_owner
from base.deploy.tests.maintenance_factory import WHEN, maintenance_agent
from base.deploy.tests.maintenance_factory import isolate as isolate


# meiva:note
# The row a departed host leaves: its owner and generation stay on the row.
def _leave_with_departed_owner(
    conn: psycopg.Connection[Any], agent_id: int, *, status: str = "idling"
) -> UUID:
    old_owner = uuid4()
    conn.execute(
        "UPDATE agents_meta SET status=%s,runtime_owner=%s,runtime_generation=%s WHERE id=%s",
        (status, old_owner, uuid4(), agent_id),
    )
    conn.commit()
    return old_owner


async def test_an_idle_agent_with_a_departed_owner_is_parked_and_readmitted(
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
    database: Database,
    machine_identity: MachineIdentity,
) -> None:
    agent = maintenance_agent(db_conn, machine_name=machine_identity.name())
    _leave_with_departed_owner(db_conn, agent)

    pause_owner.begin_maintenance("owner", WHEN)
    hold = cohort.prepare(
        db_conn,
        machine=machine_identity.name(),
        host_owner=uuid4(),
        holder="owner",
        acquired_at=WHEN,
    )

    assert hold.parked == (agent,)
    assert hold.commands == {}
    cohort.verify_drained(db_conn, hold)

    # The hold is released; the next wake goes through the ordinary admission.
    pause_owner.change_maintenance("owner", WHEN, hold, hold, resumed=True)
    assert not admission.held()
    incarnation = await admit_hosted_runtime(
        aops_pool, agent, machine_identity.name(), uuid4(), db=database
    )
    assert incarnation is not None


async def test_parking_is_the_same_when_no_host_is_running(
    db_conn: psycopg.Connection[Any], machine_identity: MachineIdentity
) -> None:
    agent = maintenance_agent(db_conn, machine_name=machine_identity.name())
    _leave_with_departed_owner(db_conn, agent)

    pause_owner.begin_maintenance("owner", WHEN)
    hold = cohort.prepare(
        db_conn,
        machine=machine_identity.name(),
        host_owner=None,
        holder="owner",
        acquired_at=WHEN,
    )

    assert hold.parked == (agent,)


def test_a_running_turn_without_a_live_owner_is_still_refused(
    db_conn: psycopg.Connection[Any], machine_identity: MachineIdentity
) -> None:
    agent = maintenance_agent(db_conn, machine_name=machine_identity.name())
    _leave_with_departed_owner(db_conn, agent, status="running")

    pause_owner.begin_maintenance("owner", WHEN)
    with pytest.raises(RuntimeError, match="live original native owner"):
        cohort.prepare(
            db_conn,
            machine=machine_identity.name(),
            host_owner=uuid4(),
            holder="owner",
            acquired_at=WHEN,
        )

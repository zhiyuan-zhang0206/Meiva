# meiva:note
# An idle agent whose owner host has departed is re-admitted by the ordinary admission.
#
# A host that exits leaves its idle agents with the owner and the generation on the
# row. Nothing wakes such an agent until a message arrives, so after a host restart it
# stays in exactly that state; the next host's admission claims it under its own owner.

from typing import Any
from uuid import uuid4

import psycopg
from psycopg_pool import AsyncConnectionPool

from agent.ownership.hosted import admit_hosted_runtime
from base.cluster.machine import MachineIdentity
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.db import Database, create_agent


async def test_an_idle_agent_with_a_departed_owner_is_readmitted_by_a_new_owner(
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
    database: Database,
    machine_identity: MachineIdentity,
) -> None:
    agent = create_agent(db_conn)
    old_owner, old_generation = uuid4(), uuid4()
    db_conn.execute(
        "INSERT INTO agents_meta(id,status,machine,runtime_owner,runtime_generation) "
        "VALUES(%s,'idling',%s,%s,%s)",
        (agent, machine_identity.name(), old_owner, old_generation),
    )
    db_conn.commit()

    new_owner = uuid4()
    incarnation = await admit_hosted_runtime(
        aops_pool, agent, machine_identity.name(), new_owner, db=database
    )

    assert incarnation is not None
    assert incarnation.owner == new_owner
    assert incarnation.generation != old_generation

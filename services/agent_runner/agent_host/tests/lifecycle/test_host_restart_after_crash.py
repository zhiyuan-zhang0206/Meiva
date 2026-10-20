from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import psycopg
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg_pool import AsyncConnectionPool

from agent.ownership.hosted import settle_running_rows_at_start
from agent.startup import wrap_saver_writes_with_nstep_interval
from base.cluster.machine import MachineIdentity
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.deploy.maintenance import admission, cohort, pause_owner
from base.deploy.tests.maintenance_factory import WHEN, maintenance_agent
from base.deploy.tests.maintenance_factory import isolate as isolate
from base.events.live.bus import EventBus
from base.lm.catalog import ModelCatalog
from base.packages.composer_commands import CommandCatalog
from services.agent_runner.agent_host.host import AgentHost
from services.agent_runner.agent_host.tests.host_policy import configured_policy


async def test_boot_settles_only_this_machines_running_rows(
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
    machine_identity: MachineIdentity,
) -> None:
    local = machine_identity.name()
    running_here = maintenance_agent(db_conn, machine_name=local)
    running_elsewhere = maintenance_agent(db_conn, machine_name="another-machine")
    idling_here = maintenance_agent(db_conn, machine_name=local)
    terminated_here = maintenance_agent(db_conn, machine_name=local)
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


async def test_a_rebooted_host_drains_a_cohort_member_prepared_for_its_predecessor(
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
    model_catalog: ModelCatalog,
    *,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    agent = maintenance_agent(db_conn, machine_name=machine_identity.name())
    crashed_boot = uuid4()
    db_conn.execute(
        "UPDATE agents_meta SET runtime_owner=%s,runtime_generation=%s WHERE id=%s",
        (crashed_boot, uuid4(), agent),
    )
    db_conn.commit()
    pause_owner.begin_maintenance("host-crash", WHEN)
    hold = cohort.prepare(
        db_conn,
        machine=machine_identity.name(),
        host_owner=crashed_boot,
        holder="host-crash",
        acquired_at=WHEN,
    )
    saver = AsyncPostgresSaver(aops_pool)
    await saver.setup()
    wrap_saver_writes_with_nstep_interval(saver, 100)
    host = AgentHost(
        policy=configured_policy(),
        pool=aops_pool,
        checkpointer=saver,
        graph=AsyncMock(),
        machine=machine_identity,
        bus=EventBus.from_settings(),
        db=Database.from_settings(gate=database_gate),
        catalog=model_catalog,
        commands=CommandCatalog(skills=list),
    )
    assert host._owner != crashed_boot

    assert await host.pending_inbound_wakes() == [agent]
    await host.run_turn(agent)

    current = admission.require_operation("host-crash", WHEN)
    assert current.maintenance is not None
    assert current.maintenance.drained == (agent,)
    cohort.verify_drained(db_conn, current.maintenance)
    assert db_conn.execute(
        "SELECT status FROM inbound_messages WHERE id=%s", (hold.commands[agent],)
    ).fetchone() == ("done",)

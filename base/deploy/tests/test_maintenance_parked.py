from typing import Any

import psycopg
import pytest

from base.agents.inbox.insert import insert_inbound_message
from base.cluster.machine import MachineIdentity
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.db import Database
from base.deploy.maintenance import cohort, pause_owner
from base.deploy.tests.maintenance_factory import WHEN, maintenance_agent
from base.deploy.tests.maintenance_factory import isolate as isolate
from base.events.live.bus import EventBus


def _parked_agent_with_a_restart_and_a_claimed_chat(
    db_conn: psycopg.Connection[Any],
    database: Database,
    event_bus: EventBus,
    machine_identity: MachineIdentity,
    *,
    restart_status: str,
) -> tuple[int, int]:
    agent = maintenance_agent(db_conn, machine_name=machine_identity.name())
    chat = insert_inbound_message(
        db_conn, agent, "unfinished action", "user", bus=event_bus, database=database
    )
    restart = insert_inbound_message(
        db_conn, agent, "", "system:update", kind="restart", bus=event_bus, database=database
    )
    db_conn.execute("UPDATE inbound_messages SET status='claimed' WHERE id=%s", (chat,))
    db_conn.execute(
        "UPDATE inbound_messages SET status=%s,claimed_at=clock_timestamp() WHERE id=%s",
        (restart_status, restart),
    )
    db_conn.commit()
    return agent, chat


def test_parked_agent_keeps_its_row_and_its_orphan_claim_is_settled(
    db_conn: psycopg.Connection[Any],
    database: Database,
    event_bus: EventBus,
    machine_identity: MachineIdentity,
) -> None:
    agent, chat = _parked_agent_with_a_restart_and_a_claimed_chat(
        db_conn, database, event_bus, machine_identity, restart_status="done"
    )
    before = db_conn.execute("SELECT * FROM agents_meta WHERE id=%s", (agent,)).fetchone()
    db_conn.commit()
    pause_owner.begin_maintenance("parked", WHEN)
    held = cohort.prepare(
        db_conn,
        machine=machine_identity.name(),
        host_owner=None,
        holder="parked",
        acquired_at=WHEN,
    )
    assert held.parked == (agent,) and held.commands == {} and held.drained == ()
    cohort.verify_drained(db_conn, held)
    assert db_conn.execute("SELECT * FROM agents_meta WHERE id=%s", (agent,)).fetchone() == before
    assert db_conn.execute(
        "SELECT status FROM inbound_messages WHERE id=%s", (chat,)
    ).fetchone() == ("done",)


def test_parked_agent_with_an_open_restart_refuses_preparation(
    db_conn: psycopg.Connection[Any],
    database: Database,
    event_bus: EventBus,
    machine_identity: MachineIdentity,
) -> None:
    _parked_agent_with_a_restart_and_a_claimed_chat(
        db_conn, database, event_bus, machine_identity, restart_status="claimed"
    )
    pause_owner.begin_maintenance("parked", WHEN)
    with pytest.raises(cohort.LifecycleCollisionError):
        cohort.prepare(
            db_conn,
            machine=machine_identity.name(),
            host_owner=None,
            holder="parked",
            acquired_at=WHEN,
        )

# meiva:note
# Real impersonation leases in the throwaway database, for the tests of the relay and of the
# commands that read and move a lease.
#
# A lease is seeded through the lease service's own door (request, prepare, activate) for an
# idle native agent on this machine, with the ACK window and the delivery budget the
# test chose written to the unit's `.env` the way an operator would. Nothing about the
# lease is faked; a test moves it on with the service's own verbs or with SQL, as the cluster
# would.

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from uuid import uuid4

import psycopg
import pytest

from base.agents import impersonation as leases
from base.agents.impersonation.status import ImpersonationStatus
from base.agents.impersonation.tests.impersonation_support import recorded_tree
from base.agents.inbox.insert import insert_inbound_message
from base.api_contracts.caller_identity import CallerIdentity
from base.cluster.machine import MachineIdentity
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.config.service_read import ConfigAuthority
from base.db import Database, create_agent
from base.events.live.bus import EventBus
from base.native_process.runtime_incarnation import RuntimeIncarnation

__all__ = [
    "START_MESSAGE",
    "ActiveSession",
    "LeaseAt",
    "active",
    "assert_message_pending",
    "delivery_attempts",
    "elapse",
    "idle_agent",
    "lease_at",
    "new_agent",
]

START_MESSAGE = "Process and ACK the test message"

type ActiveSession = tuple[dict[str, Any], RuntimeIncarnation, int]
type LeaseAt = Callable[[ImpersonationStatus], ActiveSession]


# meiva:note
# A new native agent on this machine, idle and owning its runtime: the kind of agent a
# takeover may be requested for.
def new_agent(
    db_conn: psycopg.Connection, machine: MachineIdentity
) -> tuple[int, RuntimeIncarnation]:
    agent_id = create_agent(db_conn)
    owner = RuntimeIncarnation(agent_id, uuid4(), uuid4())
    db_conn.execute(
        "INSERT INTO agents_meta(id,status,machine,runtime_generation,runtime_owner) "
        "VALUES(%s,'idling',%s,%s,%s)",
        (agent_id, machine.name(), owner.generation, owner.owner),
    )
    db_conn.commit()
    return agent_id, owner


# meiva:note
# One lease for a new idle agent, moved on to `stage` (requested, accepted or active), with
# one pending chat message in the agent's inbox. The lease dict carries the relay token the
# request minted, as the controller session received it.
def _seed(
    stage: ImpersonationStatus,
    *,
    window: int,
    attempts: int,
    db_conn: psycopg.Connection,
    unit_home: Any,
    database: Database,
    event_bus: EventBus,
    authority: ConfigAuthority,
    machine: MachineIdentity,
) -> ActiveSession:
    (unit_home / ".env").write_text(
        f"MEIVA_IMPERSONATION_ACK_WINDOW_SECONDS={window}\n"
        f"MEIVA_IMPERSONATION_MAX_DELIVERY_ATTEMPTS={attempts}\n"
    )
    agent_id, owner = new_agent(db_conn, machine)
    lease = leases.request(
        database,
        event_bus,
        agent_id,
        caller=CallerIdentity(kind="external_agent", subject="codex", instance="test"),
        ttl_seconds=3600,
        reason="Test ACK exhaustion",
        relay_provider="claude",
        process_metadata=recorded_tree(),
        authority=authority,
        local_machine=machine.name(),
        name="Test name",
        executor_name="Test executor_name",
    )
    assert (lease["ack_window_seconds"], lease["max_delivery_attempts"]) == (window, attempts)
    if stage is not ImpersonationStatus.REQUESTED:
        leases.prepare(
            database,
            event_bus,
            lease["id"],
            agent_id,
            owner,
            START_MESSAGE,
            local_machine=machine.name(),
        )
    if stage is ImpersonationStatus.ACTIVE:
        leases.activate(database, event_bus, lease["id"], owner)
    token = lease["relay_token"]
    lease = leases.relay_get(
        database, event_bus, lease["id"], token, local_machine=machine.name()
    ) | {"relay_token": token}
    assert lease["status"] == stage
    message_id = insert_inbound_message(
        db_conn,
        agent_id,
        "Unprocessed user input",
        "user",
        kind="chat",
        bus=event_bus,
        database=database,
    )
    db_conn.commit()
    if stage is ImpersonationStatus.ACTIVE:
        leases.relay_inbox(database, lease["id"], token, local_machine=machine.name())
    return lease, owner, message_id


@pytest.fixture
def active(
    db_conn: psycopg.Connection,
    request: pytest.FixtureRequest,
    unit_home: Any,
    database: Database,
    event_bus: EventBus,
    *,
    config_authority: ConfigAuthority,
    machine_identity: MachineIdentity,
) -> ActiveSession:
    """An active lease whose ACK window and delivery budget are the test's (180 s, 2 by default)."""
    window, attempts = getattr(request, "param", (180, 2))
    return _seed(
        ImpersonationStatus.ACTIVE,
        window=window,
        attempts=attempts,
        db_conn=db_conn,
        unit_home=unit_home,
        database=database,
        event_bus=event_bus,
        authority=config_authority,
        machine=machine_identity,
    )


@pytest.fixture
def idle_agent(
    db_conn: psycopg.Connection, event_bus: EventBus, *, machine_identity: MachineIdentity
) -> tuple[int, RuntimeIncarnation]:
    """A new idle native agent on this machine, with no lease; the cluster's Redis is running."""
    del event_bus
    return new_agent(db_conn, machine_identity)


@pytest.fixture
def lease_at(
    db_conn: psycopg.Connection,
    unit_home: Any,
    database: Database,
    event_bus: EventBus,
    *,
    config_authority: ConfigAuthority,
    machine_identity: MachineIdentity,
) -> LeaseAt:
    """Seed a lease for a new agent at the requested, accepted or active stage."""

    def seed(stage: ImpersonationStatus) -> ActiveSession:
        return _seed(
            stage,
            window=180,
            attempts=2,
            db_conn=db_conn,
            unit_home=unit_home,
            database=database,
            event_bus=event_bus,
            authority=config_authority,
            machine=machine_identity,
        )

    return seed


# meiva:note
# Age the lease's deliveries by `seconds`, as if that long had passed since they were pushed:
# the database owns the ACK windows, so this is how a test crosses one without waiting.
def elapse(db_conn: psycopg.Connection, lease: dict[str, Any], seconds: int = 181) -> None:
    db_conn.execute(
        "UPDATE agent_impersonation_messages SET last_delivery_at="
        "clock_timestamp() - %s*interval '1 second' WHERE lease_id=%s AND delivery_attempts>0",
        (seconds, lease["id"]),
    )
    db_conn.commit()


def delivery_attempts(db_conn: psycopg.Connection, message_id: int) -> int:
    row = db_conn.execute(
        "SELECT delivery_attempts FROM agent_impersonation_messages WHERE inbound_id=%s",
        (message_id,),
    ).fetchone()
    assert row is not None
    return row[0]


def assert_message_pending(db_conn: psycopg.Connection, message_id: int) -> None:
    assert db_conn.execute(
        "SELECT status FROM inbound_messages WHERE id=%s", (message_id,)
    ).fetchone() == ("pending",)

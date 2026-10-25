# meiva:note
# `deliver_chat_inbound`: the durable inbound is committed first, then announced, and a failed
# announcement never undoes or hides it.
#
# Every collaborator is real. A Redis that is down is an address nothing listens on; an error the
# publisher does not absorb is a bus pointed at a malformed Redis URL; an agent's home is the
# gateway's own machine served by the real ops daemon (`gateway_local_runner`) or a runner on a
# real listener (`runner_ops`), so a wake of a terminated agent is the real resurrect against the
# test's Postgres.

from __future__ import annotations

import asyncio
import threading
from collections.abc import Generator

import psycopg
import pytest
from psycopg.rows import TupleRow
from psycopg_pool import ConnectionPool

from base.agents.inbox.chat_delivery import ChatInboundCommittedError
from base.api_contracts.agent_contract import AgentStatus
from base.cluster.machine import MachineFacts, MachineIdentityReader
from base.cluster.machine.configured import ConfiguredMachineIdentity
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.config import settings
from base.config.service_read import ConfigAuthority
from base.db import Database, create_agent
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus, EventBusConfig
from base.events.live.tests.live_capture import snapshots_at_events
from base.host.system.tests.fake_commands import fake_host
from base.lm.catalog import ModelCatalog
from gateway.cluster.process_boot import LOADED_IMAGE
from gateway.tests.events.sse_support import refused_redis_url
from gateway.tests.support.live_events.faults import unpublishable_bus
from gateway.tests.support.runner_ops.local_runner import local_runner
from gateway.tests.support.runner_ops.ops_stub import (
    OpsCall,
    OpsReply,
    RunnerOps,
    completed,
    runner_ops,
)
from ops.lifecycle import delivery
from ops.lifecycle.delivery import deliver_chat_inbound


def _sync_pool() -> ConnectionPool[psycopg.Connection[TupleRow]]:
    return ConnectionPool(settings.data_plane.db_url, min_size=1, max_size=2, open=False)


def _seed_agent(
    conn: psycopg.Connection, status: str, *, machine: str, suppressed: bool = False
) -> int:
    tid = create_agent(conn)
    conn.execute(
        "INSERT INTO agents_meta (id, status, machine, wake_suppressed_until, "
        "wake_suppress_reason) VALUES (%s, %s, %s, "
        "CASE WHEN %s THEN now() + interval '1 hour' END, "
        "CASE WHEN %s THEN 'resurrect_failed' END) "
        "ON CONFLICT (id) DO UPDATE SET status = EXCLUDED.status, machine = EXCLUDED.machine, "
        "wake_suppressed_until = EXCLUDED.wake_suppressed_until, "
        "wake_suppress_reason = EXCLUDED.wake_suppress_reason",
        (tid, status, machine, suppressed, suppressed),
    )
    conn.commit()
    return tid


def _inbound_kinds(conn: psycopg.Connection, agent_id: int) -> list[tuple[str, int]]:
    rows = conn.execute(
        "SELECT kind, count(*) FROM inbound_messages WHERE agent_id = %s "
        "GROUP BY kind ORDER BY kind",
        (agent_id,),
    ).fetchall()
    conn.commit()
    return [(kind, count) for kind, count in rows]


def _bus(redis_url: str) -> EventBus:
    return EventBus(EventBusConfig(redis_url=redis_url, events_channel="delivery-test"))


# meiva:note
# The gateway's own machine, serving as an agent-runner through its in-process ops route: a
# terminated agent homed here is resurrected by the real op.
@pytest.fixture
def local_home(
    machine_identity: ConfiguredMachineIdentity, gateway_local_runner: RunnerOps
) -> ConfiguredMachineIdentity:
    machine_identity.set(role="agent-runner")
    return machine_identity


# meiva:note
# Another machine serving as an agent-runner through the same real ops daemon stack.
@pytest.fixture
def peer_home(
    db_conn: psycopg.Connection,
    database: Database,
    config_authority: ConfigAuthority,
    model_catalog: ModelCatalog,
) -> Generator[str]:
    peer = MachineFacts.of(
        name="peer-home",
        role=frozenset({"agent-runner"}),
        host="localhost",
        description=None,
    )
    with local_runner(
        db_conn,
        database=database,
        authority=config_authority,
        catalog=model_catalog,
        machine=peer,
        host=fake_host(),
        image=LOADED_IMAGE,
    ):
        yield peer.name()


# meiva:note
# A Redis that is down is a known transport failure: the delivery is stored and answered, and
# nothing is raised for the announcements that could not be sent.
async def test_deliver_survives_publish_failure(
    db_conn: psycopg.Connection,
    database_gate: ProcessDbGate,
    *,
    machine_identity: MachineIdentityReader,
) -> None:
    tid = _seed_agent(db_conn, "idling", machine=machine_identity.name())

    with _sync_pool() as pool:
        await deliver_chat_inbound(
            pool,
            Database.from_settings(gate=database_gate),
            _bus(refused_redis_url()),
            tid,
            prepare=lambda _c: "hello there",
            refresh_badge=True,
            local_machine=machine_identity.name(),
        )

    rows = db_conn.execute("SELECT content FROM inbound_messages WHERE agent_id = %s", (tid,))
    assert rows.fetchall() == [("hello there",)]


# meiva:note
# An error the publisher does not absorb (a bus that cannot even build its client) is not hidden
# after the commit: it surfaces as `ChatInboundCommittedError` carrying the durable receipt. The
# first delivery announces the new inbound; the retry of the same message skips that wake and
# reaches the badge refresh.
@pytest.mark.parametrize("step", ["new_inbound", "badge_of_a_retry"])
async def test_unknown_publish_error_exposes_committed_delivery(
    db_conn: psycopg.Connection,
    event_bus: EventBus,
    database_gate: ProcessDbGate,
    step: str,
    *,
    machine_identity: MachineIdentityReader,
) -> None:
    tid = _seed_agent(db_conn, "idling", machine=machine_identity.name())
    db = Database.from_settings(gate=database_gate)
    with _sync_pool() as pool:
        if step == "badge_of_a_retry":
            first = await deliver_chat_inbound(
                pool,
                db,
                event_bus,
                tid,
                prepare=lambda _c: "hi",
                client_message_id="badge-committed",
                local_machine=machine_identity.name(),
            )
            assert first.inbound_id is not None
        with pytest.raises(ChatInboundCommittedError, match="committed; post-commit work failed"):
            await deliver_chat_inbound(
                pool,
                db,
                unpublishable_bus(),
                tid,
                prepare=lambda _c: "hi",
                refresh_badge=True,
                client_message_id="badge-committed",
                local_machine=machine_identity.name(),
            )
    rows = db_conn.execute("SELECT id, content FROM inbound_messages WHERE agent_id = %s", (tid,))
    stored = rows.fetchall()
    assert [content for _id, content in stored] == ["hi"]


# meiva:note
# The inbound a delivery commits is the guard of the wake it asks for: a terminated agent whose
# home is the gateway's machine is resurrected by the real op, which accepts the chat as its
# trigger.
async def test_deliver_wakes_a_terminated_agent_with_the_inserted_chat_as_guard(
    db_conn: psycopg.Connection,
    database_gate: ProcessDbGate,
    event_bus: EventBus,
    local_home: MachineIdentityReader,
) -> None:
    tid = _seed_agent(db_conn, "terminated", machine=local_home.name())

    with _sync_pool() as pool:
        result = await deliver_chat_inbound(
            pool,
            Database.from_settings(gate=database_gate),
            event_bus,
            tid,
            prepare=lambda _c: "guard me",
            local_machine=local_home.name(),
        )

    chat = db_conn.execute(
        "SELECT id FROM inbound_messages WHERE agent_id = %s AND kind = 'chat'", (tid,)
    ).fetchone()
    assert chat is not None and result.inbound_id == chat[0]
    assert result.status is AgentStatus.IDLING
    status = db_conn.execute("SELECT status FROM agents_meta WHERE id = %s", (tid,)).fetchone()
    assert status == ("idling",)
    assert _inbound_kinds(db_conn, tid) == [("chat", 1), ("resurrect", 1)]


# meiva:note
# A retry of the same client message delivers one chat and one resurrection.
async def test_retried_client_message_resurrects_terminated_agent_once(
    db_conn: psycopg.Connection,
    database_gate: ProcessDbGate,
    event_bus: EventBus,
    local_home: MachineIdentityReader,
) -> None:
    tid = _seed_agent(db_conn, "terminated", machine=local_home.name())

    with _sync_pool() as pool:
        deliveries = [
            await deliver_chat_inbound(
                pool,
                Database.from_settings(gate=database_gate),
                event_bus,
                tid,
                prepare=lambda _c: "wake once",
                client_message_id="client-wake-once",
                local_machine=local_home.name(),
            )
            for _ in range(2)
        ]

    assert deliveries[0].inbound_id == deliveries[1].inbound_id
    assert _inbound_kinds(db_conn, tid) == [("chat", 1), ("resurrect", 1)]


# meiva:note
# A fresh message from a peer agent wakes the owner even though an earlier failed resurrection
# left the agent's wake suppressed: the agent's home machine (served by the real ops daemon)
# resurrects it, clears the suppression, and the delivery is audited as the peer's send.
async def test_fresh_peer_message_wakes_owner_despite_previous_suppression(
    db_conn: psycopg.Connection,
    database_gate: ProcessDbGate,
    event_bus: EventBus,
    peer_home: str,
    *,
    machine_identity: MachineIdentityReader,
) -> None:
    tid = _seed_agent(db_conn, "terminated", machine=peer_home, suppressed=True)

    with _sync_pool() as pool:
        result = await deliver_chat_inbound(
            pool,
            Database.from_settings(gate=database_gate),
            event_bus,
            tid,
            prepare=lambda _conn: "peer work",
            source="agent:42",
            local_machine=machine_identity.name(),
        )

    assert result.status is AgentStatus.IDLING
    assert result.inbound_id is not None
    row = db_conn.execute(
        "SELECT status,kind,source,content FROM inbound_messages WHERE id=%s",
        (result.inbound_id,),
    ).fetchone()
    assert row == ("pending", "chat", "agent:42", "peer work")
    assert db_conn.execute(
        "SELECT status, wake_suppressed_until IS NULL FROM agents_meta WHERE id=%s", (tid,)
    ).fetchone() == ("idling", True)
    audited = db_conn.execute(
        "SELECT target_agent_id, attributes->>'inbound_id' FROM audit_events "
        "WHERE agent_id=%s AND event_name='send_message'",
        (tid,),
    ).fetchall()
    assert audited == [(42, str(result.inbound_id))]


# meiva:note
# Two deliveries of the same client message racing on a terminated agent produce one chat and
# one resurrection: the home's compare-and-set admits a single wake.
async def test_concurrent_same_key_terminated_delivery_has_one_resurrect_effect(
    db_conn: psycopg.Connection,
    database_gate: ProcessDbGate,
    event_bus: EventBus,
    local_home: MachineIdentityReader,
) -> None:
    tid = _seed_agent(db_conn, "terminated", machine=local_home.name())

    with _sync_pool() as pool:
        first, second = await asyncio.gather(
            *(
                deliver_chat_inbound(
                    pool,
                    Database.from_settings(gate=database_gate),
                    event_bus,
                    tid,
                    prepare=lambda _c: "concurrent wake",
                    client_message_id="client-concurrent-wake",
                    local_machine=local_home.name(),
                )
                for _ in range(2)
            )
        )

    assert first.inbound_id == second.inbound_id
    assert _inbound_kinds(db_conn, tid) == [("chat", 1), ("resurrect", 1)]


# meiva:note
# The badge hint is published after the delivery commits: a subscriber that reads the database
# the moment the hint arrives, over its own connection, finds the committed change.
async def test_badge_publish_happens_after_commit(
    db_conn: psycopg.Connection,
    database_gate: ProcessDbGate,
    event_bus: EventBus,
    *,
    machine_identity: MachineIdentityReader,
) -> None:
    tid = _seed_agent(db_conn, "idling", machine=machine_identity.name())

    def label_seen_by_a_separate_connection() -> str | None:
        with psycopg.connect(settings.data_plane.db_url) as probe:
            row = probe.execute("SELECT label FROM agents WHERE id = %s", (tid,)).fetchone()
        return None if row is None else row[0]

    def _prepare(conn: psycopg.Connection) -> None:
        conn.execute("UPDATE agents SET label = 'ordering-marker' WHERE id = %s", (tid,))

    with (
        snapshots_at_events(event_bus, label_seen_by_a_separate_connection) as seen,
        _sync_pool() as pool,
    ):
        await deliver_chat_inbound(
            pool,
            Database.from_settings(gate=database_gate),
            event_bus,
            tid,
            prepare=_prepare,
            refresh_badge=True,
            local_machine=machine_identity.name(),
        )

    badges = [
        label
        for event, label in seen
        if event["role"] == "agent_updated" and event["agent_id"] == tid
    ]
    assert badges, "the badge hint was never published"
    assert set(badges) == {"ordering-marker"}, (
        "badge publish saw uncommitted state — it ran before the delivery commit"
    )


# meiva:note
# A caller cancelled after the commit, while the wake of the terminated agent is still in flight
# to its home, leaves the committed chat behind; reconciling the same client message returns the
# same receipt and finishes the wake.
async def test_cancelled_caller_leaves_the_committed_chat_and_reconciles_same_receipt(
    db_conn: psycopg.Connection,
    database_gate: ProcessDbGate,
    event_bus: EventBus,
    *,
    machine_identity: MachineIdentityReader,
) -> None:
    tid = _seed_agent(db_conn, "terminated", machine="slow-home")
    wake_arrived = threading.Event()
    release = threading.Event()

    def hold_the_wake(_call: OpsCall) -> OpsReply:
        wake_arrived.set()
        assert release.wait(timeout=30), "the held wake was never released"
        return completed({"status": "already_alive"})

    db = Database.from_settings(gate=database_gate)
    with runner_ops(db_conn, "slow-home", {"lifecycle": hold_the_wake}), _sync_pool() as pool:
        caller = asyncio.create_task(
            deliver_chat_inbound(
                pool,
                db,
                event_bus,
                tid,
                prepare=lambda _c: "cancel after commit",
                client_message_id="cancel-committed",
                local_machine=machine_identity.name(),
            )
        )
        assert await asyncio.to_thread(wake_arrived.wait, 10), "the wake never reached the home"
        row = db_conn.execute(
            "SELECT id FROM inbound_messages WHERE agent_id=%s AND content=%s",
            (tid, "cancel after commit"),
        ).fetchone()
        assert row is not None
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        release.set()
        recovered = await delivery.reconcile_chat_delivery(
            pool,
            db,
            event_bus,
            tid,
            local_machine=machine_identity.name(),
            client_message_id="cancel-committed",
            content="cancel after commit",
            source="user",
            payload=None,
        )
    assert recovered is not None and recovered.inbound_id == row[0]
    assert db_conn.execute(
        "SELECT count(*) FROM inbound_messages WHERE agent_id=%s", (tid,)
    ).fetchone() == (1,)

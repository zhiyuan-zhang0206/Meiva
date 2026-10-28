# meiva:note
# Database-enforced delivery budget, races and native handoff.

import json
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import psycopg
import pytest
import redis

from base.agents import impersonation as leases
from base.agents.impersonation import delivery as delivery
from base.agents.inbox.insert import insert_inbound_message
from base.clock import SYSTEM_TIMERS
from base.clock.tests.seeded_rng import seeded_rng
from base.cluster import inbound_channel
from base.cluster.machine import MachineIdentityReader
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from cli.commands.agents.tests.lease_support import ActiveSession as ActiveSession
from cli.commands.agents.tests.lease_support import active as active
from cli.commands.agents.tests.lease_support import assert_message_pending, elapse


def reserve(
    lease: dict[str, Any],
    message_id: int,
    *,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentityReader,
) -> frozenset[int]:
    return delivery.reserve_delivery(
        Database.from_settings(gate=database_gate),
        EventBus.from_settings(rng=seeded_rng(), timers=SYSTEM_TIMERS),
        lease["id"],
        lease["relay_token"],
        [message_id],
        local_machine=machine_identity.name(),
    )


def assert_expiry_note_explains_pending_messages(
    db_conn: psycopg.Connection, summary_inbound_id: int
) -> None:
    note = db_conn.execute(
        "SELECT content FROM inbound_messages WHERE id=%s", (summary_inbound_id,)
    ).fetchone()
    assert note is not None
    assert "did not ACK" in note[0] and "Unacknowledged messages remain pending" in note[0]


def _assert_exhaustion_preserved(
    ended: dict[str, Any],
    lease: dict[str, Any],
    mid: int,
    db_conn: psycopg.Connection,
    *,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentityReader,
) -> None:
    assert ended["status"] == "active"
    assert f"message {mid} exhausted" in ended["relay_degraded_reason"]
    assert ended["reason"] == "Test ACK exhaustion"
    assert (
        reserve(lease, mid, database_gate=database_gate, machine_identity=machine_identity) == set()
    )
    assert_message_pending(db_conn, mid)
    assert ended["expires_at"] == lease["expires_at"]


def test_two_attempts_keep_lease_active_and_late_receipt_possible(
    db_conn: psycopg.Connection,
    active: ActiveSession,
    database: Database,
    event_bus: EventBus,
    *,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentityReader,
) -> None:
    lease, owner, mid = active
    assert (lease["ack_window_seconds"], lease["max_delivery_attempts"]) == (180, 2)
    assert reserve(lease, mid, database_gate=database_gate, machine_identity=machine_identity) == {
        mid
    }
    assert (
        reserve(lease, mid, database_gate=database_gate, machine_identity=machine_identity) == set()
    )  # no early retry
    elapse(db_conn, lease)
    assert (
        leases.relay_get(
            database,
            event_bus,
            lease["id"],
            lease["relay_token"],
            local_machine=machine_identity.name(),
        )["status"]
        == "active"
    )
    assert reserve(lease, mid, database_gate=database_gate, machine_identity=machine_identity) == {
        mid
    }
    elapse(db_conn, lease, 179)
    assert (
        leases.relay_get(
            database,
            event_bus,
            lease["id"],
            lease["relay_token"],
            local_machine=machine_identity.name(),
        )["status"]
        == "active"
    )
    assert (
        reserve(lease, mid, database_gate=database_gate, machine_identity=machine_identity) == set()
    )
    elapse(db_conn, lease)
    # The native reconciler also enforces the budget without a live relay.
    ended = leases.native_status(database, event_bus, owner.agent_id, owner)
    assert ended is not None
    _assert_exhaustion_preserved(
        ended, lease, mid, db_conn, database_gate=database_gate, machine_identity=machine_identity
    )
    leases.ack(
        database,
        event_bus,
        lease["id"],
        [mid],
        local_machine=machine_identity.name(),
    )
    assert db_conn.execute(
        "SELECT status FROM inbound_messages WHERE id=%s", (mid,)
    ).fetchone() == ("done",)


# meiva:note
# Everything published on `channels` while `act` runs, read from a real Redis subscription: the
# frames the cluster's other processes would see.
def _published(
    redis_url: str, channels: list[str], act: Callable[[], object]
) -> list[tuple[str, str]]:
    # meiva:note
    # redis-py leaves these factories' **kwargs untyped; every supplied argument is explicit.
    client = redis.Redis.from_url(redis_url, decode_responses=True)  # pyright: ignore[reportUnknownMemberType]
    pubsub = client.pubsub()  # pyright: ignore[reportUnknownMemberType]
    pubsub.subscribe(*channels)
    try:
        for _ in channels:
            assert pubsub.get_message(timeout=5.0) is not None
        act()
        frames: list[tuple[str, str]] = []
        while (message := pubsub.get_message(timeout=0.5)) is not None:
            if message["type"] == "message":
                channel, data = message["channel"], message["data"]
                assert isinstance(channel, str) and isinstance(data, str)
                frames.append((channel, data))
        return frames
    finally:
        pubsub.close()
        client.close()


def test_delivery_reservation_that_expires_lease_refreshes_roster(
    db_conn: psycopg.Connection,
    active: ActiveSession,
    event_bus: EventBus,
    redis_url: str,
    *,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentityReader,
) -> None:
    lease, owner, message_id = active
    db_conn.execute(
        "UPDATE agent_impersonations SET expires_at=clock_timestamp()-interval '1 second' "
        "WHERE id=%s",
        (lease["id"],),
    )
    db_conn.commit()
    live = event_bus.channel
    wake = inbound_channel(owner.agent_id, live)

    def attempt() -> frozenset[int]:
        return reserve(
            lease, message_id, database_gate=database_gate, machine_identity=machine_identity
        )

    first: list[frozenset[int]] = []
    frames = _published(redis_url, [live, wake], lambda: first.append(attempt()))
    assert first == [frozenset()]
    announced = sorted(json.loads(data)["role"] for channel, data in frames if channel == live)
    assert announced == ["agent_updated", "impersonation_changed"]
    assert {json.loads(data)["agent_id"] for channel, data in frames if channel == live} == {
        owner.agent_id
    }
    assert [channel for channel, _ in frames].count(wake) == 1

    # meiva:note
    # The lease has ended: reserving again changes nothing and announces nothing.
    assert _published(redis_url, [live, wake], attempt) == []


def test_ack_in_final_window_prevents_expiry(
    db_conn: psycopg.Connection,
    active: ActiveSession,
    database: Database,
    event_bus: EventBus,
    *,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentityReader,
) -> None:
    lease, _, mid = active
    reserve(lease, mid, database_gate=database_gate, machine_identity=machine_identity)
    elapse(db_conn, lease)
    reserve(lease, mid, database_gate=database_gate, machine_identity=machine_identity)
    leases.ack(
        database,
        event_bus,
        lease["id"],
        [mid],
        local_machine=machine_identity.name(),
    )
    elapse(db_conn, lease)
    assert (
        leases.relay_get(
            database,
            event_bus,
            lease["id"],
            lease["relay_token"],
            local_machine=machine_identity.name(),
        )["status"]
        == "active"
    )
    assert (
        reserve(lease, mid, database_gate=database_gate, machine_identity=machine_identity) == set()
    )


def test_rotating_relay_does_not_reset_budget(
    db_conn: psycopg.Connection,
    active: ActiveSession,
    database: Database,
    event_bus: EventBus,
    *,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentityReader,
) -> None:
    lease, owner, mid = active
    reserve(lease, mid, database_gate=database_gate, machine_identity=machine_identity)
    elapse(db_conn, lease)
    reserve(lease, mid, database_gate=database_gate, machine_identity=machine_identity)
    leases.provision_relay(database, lease["id"], owner, "replacement")
    with pytest.raises(leases.ImpersonationError, match="Invalid relay token"):
        reserve(lease, mid, database_gate=database_gate, machine_identity=machine_identity)
    lease["relay_token"] = "replacement"  # noqa: S105 — test-only scoped credential
    rows = leases.relay_inbox(
        database, lease["id"], "replacement", local_machine=machine_identity.name()
    )
    assert rows[0]["delivery_attempts"] == 2
    assert rows[0]["delivery_due"] is False
    elapse(db_conn, lease)
    assert (
        reserve(lease, mid, database_gate=database_gate, machine_identity=machine_identity) == set()
    )
    assert (
        leases.relay_get(
            database, event_bus, lease["id"], "replacement", local_machine=machine_identity.name()
        )["status"]
        == "active"
    )


def test_concurrent_relays_only_reserve_once(
    db_conn: psycopg.Connection,
    active: ActiveSession,
    database: Database,
    *,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentityReader,
) -> None:
    lease, _, mid = active
    with ThreadPoolExecutor(2) as pool:
        futures = [
            pool.submit(
                reserve, lease, mid, database_gate=database_gate, machine_identity=machine_identity
            )
            for _ in range(2)
        ]
    assert sorted(len(f.result()) for f in futures) == [0, 1]
    assert (
        leases.relay_inbox(
            database, lease["id"], lease["relay_token"], local_machine=machine_identity.name()
        )[0]["delivery_attempts"]
        == 1
    )


def test_ack_after_read_before_reservation_cannot_be_pushed(
    db_conn: psycopg.Connection,
    active: ActiveSession,
    database: Database,
    event_bus: EventBus,
    *,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentityReader,
) -> None:
    lease, _, mid = active
    leases.ack(
        database,
        event_bus,
        lease["id"],
        [mid],
        local_machine=machine_identity.name(),
    )
    assert (
        reserve(lease, mid, database_gate=database_gate, machine_identity=machine_identity) == set()
    )


def test_release_wins_timeout_race_without_being_overwritten(
    db_conn: psycopg.Connection,
    active: ActiveSession,
    database: Database,
    event_bus: EventBus,
    *,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentityReader,
) -> None:
    lease, _, mid = active
    reserve(lease, mid, database_gate=database_gate, machine_identity=machine_identity)
    elapse(db_conn, lease)
    reserve(lease, mid, database_gate=database_gate, machine_identity=machine_identity)
    leases.release(
        database,
        event_bus,
        lease["id"],
        "Returning control",
        local_machine=machine_identity.name(),
    )
    elapse(db_conn, lease)
    assert (
        reserve(lease, mid, database_gate=database_gate, machine_identity=machine_identity) == set()
    )
    assert (
        leases.relay_get(
            database,
            event_bus,
            lease["id"],
            lease["relay_token"],
            local_machine=machine_identity.name(),
        )["status"]
        == "released"
    )


def test_exhausted_message_outside_page_remains_visible_without_ending_lease(
    db_conn: psycopg.Connection,
    active: ActiveSession,
    database: Database,
    event_bus: EventBus,
    *,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentityReader,
) -> None:
    lease, _, first = active
    other = insert_inbound_message(
        db_conn,
        lease["agent_id"],
        "Second message",
        "user",
        kind="chat",
        bus=event_bus,
        database=database,
    )
    db_conn.commit()
    leases.relay_inbox(
        database, lease["id"], lease["relay_token"], local_machine=machine_identity.name()
    )
    reserve(lease, other, database_gate=database_gate, machine_identity=machine_identity)
    elapse(db_conn, lease)
    reserve(lease, other, database_gate=database_gate, machine_identity=machine_identity)
    rows = leases.relay_inbox(
        database, lease["id"], lease["relay_token"], limit=1, local_machine=machine_identity.name()
    )
    assert [row["id"] for row in rows] == [first]
    # Fresh input and ACK of another message cannot forgive the exhausted one.
    reserve(lease, first, database_gate=database_gate, machine_identity=machine_identity)
    leases.ack(
        database,
        event_bus,
        lease["id"],
        [first],
        local_machine=machine_identity.name(),
    )
    elapse(db_conn, lease)
    ended = leases.relay_get(
        database,
        event_bus,
        lease["id"],
        lease["relay_token"],
        local_machine=machine_identity.name(),
    )
    assert ended["status"] == "active"
    assert f"message {other}" in ended["relay_degraded_reason"]

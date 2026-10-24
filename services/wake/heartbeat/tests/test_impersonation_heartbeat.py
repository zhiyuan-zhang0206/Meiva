import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from base import telemetry
from base.agents import impersonation as leases
from base.cluster.machine import MachineIdentity
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.config import settings
from base.config.service_read import ConfigAuthority
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.lm.catalog import ModelCatalog
from base.tests.impersonation.support import accept_controller, create_native_owner
from meiva.sdk_surface.installation import Installation
from services.wake.heartbeat.daemon import (
    reconcile_checkin_outcomes,
    send_heartbeat_checkin,
)
from services.wake.heartbeat.selection import select_idle_agents_needing_heartbeat
from services.wake.heartbeat.tests.heartbeat_support import lease as lease_row
from services.wake.heartbeat.tests.heartbeat_support import make_idle


@pytest.fixture
def pool(db_url: str):
    with ConnectionPool(settings.data_plane.db_url, min_size=1, max_size=2) as pool:
        yield pool


def active_lease(
    conn: psycopg.Connection,
    *,
    config_authority: ConfigAuthority,
    age: float = 400,
    database_gate: ProcessDbGate,
    local_machine: str,
) -> dict[str, Any]:
    lease = accept_controller(
        create_native_owner(conn, local_machine=local_machine),
        authority=config_authority,
        database_gate=database_gate,
        local_machine=local_machine,
    )
    conn.execute(
        "UPDATE agent_impersonations SET activated_at=now()-make_interval(secs=>%s) WHERE id=%s",
        (age, lease["id"]),
    )
    conn.commit()
    return lease


@pytest.fixture
def event_pipeline(
    database: Database, machine_identity: MachineIdentity, unit_home: Path
) -> Iterator[telemetry.EventPipeline]:
    """Own the process writer used by the service operation and mirror barrier."""
    pipeline = telemetry.build_pipeline(
        database=lambda: database, machine=machine_identity, home=unit_home
    )
    try:
        telemetry.init_telemetry(
            process="test-wake-service", pipeline=pipeline, machine_reader=machine_identity.name
        )
        yield pipeline
    finally:
        pipeline.stop(timeout=2)


def selected(pool: ConnectionPool) -> dict[int, float]:
    return {
        agent_id: idle_minutes
        for agent_id, idle_minutes, _reason in select_idle_agents_needing_heartbeat(
            pool, 300, heartbeat_interval_s=300, now=time.time()
        )
    }


# meiva:note
# agent_id -> why the scan picked it (`None` for the ordinary cadence).
def selected_with_reason(
    pool: ConnectionPool, *, corpse_grace_s: float = 900.0
) -> dict[int, str | None]:
    return {
        agent_id: reason
        for agent_id, _idle_minutes, reason in select_idle_agents_needing_heartbeat(
            pool, 300, heartbeat_interval_s=300, now=time.time(), corpse_grace_s=corpse_grace_s
        )
    }


@pytest.mark.parametrize("status", ["idling", "running"])
def test_active_clock_does_not_depend_on_native_turns(
    redis_url: str,
    pool: ConnectionPool,
    db_conn: psycopg.Connection,
    status: str,
    config_authority: ConfigAuthority,
    *,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
):
    lease = active_lease(
        db_conn,
        config_authority=config_authority,
        database_gate=database_gate,
        local_machine=machine_identity.name(),
    )
    db_conn.execute(
        "UPDATE agents_meta SET status=%s,last_active_at=now(),heartbeat_backoff_level=5 WHERE id=%s",
        (status, lease["agent_id"]),
    )
    db_conn.commit()
    assert selected(pool)[lease["agent_id"]] == pytest.approx(400 / 60, abs=0.1)


def test_activation_starts_the_clock(
    redis_url: str,
    pool: ConnectionPool,
    db_conn: psycopg.Connection,
    config_authority: ConfigAuthority,
    *,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
):
    lease = active_lease(
        db_conn,
        age=20,
        config_authority=config_authority,
        database_gate=database_gate,
        local_machine=machine_identity.name(),
    )
    db_conn.execute(
        "UPDATE agents_meta SET last_active_at=now()-interval '1 hour' WHERE id=%s",
        (lease["agent_id"],),
    )
    db_conn.commit()
    assert lease["agent_id"] not in selected(pool)


def test_pause_then_normal_cadence(
    pool: ConnectionPool,
    db_conn: psycopg.Connection,
    database: Database,
    event_bus: EventBus,
    config_authority: ConfigAuthority,
    *,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
):
    lease = active_lease(
        db_conn,
        config_authority=config_authority,
        database_gate=database_gate,
        local_machine=machine_identity.name(),
    )
    aid = lease["agent_id"]
    db_conn.execute(
        "UPDATE agents_meta SET heartbeat_paused_until=now()+interval '1 hour' WHERE id=%s", (aid,)
    )
    db_conn.commit()
    assert aid not in selected(pool)
    db_conn.execute(
        "UPDATE agents_meta SET heartbeat_paused_until=now()-interval '1 second' WHERE id=%s",
        (aid,),
    )
    db_conn.commit()
    assert aid in selected(pool)
    send_heartbeat_checkin(pool, database, event_bus, aid, 400 / 60)
    messages = leases.inbox(database, lease["id"], local_machine=machine_identity.name())
    assert [m["kind"] for m in messages] == ["heartbeat"]
    leases.ack(
        database,
        event_bus,
        lease["id"],
        [messages[0]["id"]],
        local_machine=machine_identity.name(),
    )
    assert aid not in selected(pool)
    db_conn.execute(
        "UPDATE agents_meta SET last_heartbeat_at=now()-interval '301 seconds' WHERE id=%s", (aid,)
    )
    db_conn.commit()
    assert aid in selected(pool)


def test_external_work_is_not_a_failed_native_turn(
    redis_url: str,
    pool: ConnectionPool,
    db_conn: psycopg.Connection,
    config_authority: ConfigAuthority,
    *,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
):
    lease = active_lease(
        db_conn,
        config_authority=config_authority,
        database_gate=database_gate,
        local_machine=machine_identity.name(),
    )
    aid = lease["agent_id"]
    pending, failures, noops = {aid: 400 / 60}, {aid: 2}, {aid: 2}
    reconcile_checkin_outcomes(
        pool,
        pending_checkin=pending,
        failure_streak=failures,
        noop_streak=noops,
        idle_threshold_s=300,
        noop_nudges_threshold=1,
    )
    assert pending == failures == noops == {}
    assert db_conn.execute(
        "SELECT heartbeat_backoff_level FROM agents_meta WHERE id=%s", (aid,)
    ).fetchone() == (0,)


def test_expired_active_lease_does_not_receive_checkin(
    redis_url: str,
    pool: ConnectionPool,
    db_conn: psycopg.Connection,
    config_authority: ConfigAuthority,
    *,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
):
    lease = active_lease(
        db_conn,
        config_authority=config_authority,
        database_gate=database_gate,
        local_machine=machine_identity.name(),
    )
    db_conn.execute(
        "UPDATE agent_impersonations SET expires_at=now()-interval '1 second' WHERE id=%s",
        (lease["id"],),
    )
    db_conn.commit()
    assert lease["agent_id"] not in selected(pool)


def test_sdk_attachment_can_pause_heartbeat(
    redis_url: str,
    pool: ConnectionPool,
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
    config_authority: ConfigAuthority,
    model_installation: Installation,
    *,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
):
    import meiva
    from base.agents.context.external_attachment import plugin_state_codec
    from base.agents.context.external_attachment.state_schema import BaseAgentState
    from meiva import impersonation
    from meiva.tests.fixtures.pin_agent import pin_agent

    monkeypatch.setattr(meiva, "__plugin_installation__", model_installation, raising=False)

    lease = active_lease(
        db_conn,
        config_authority=config_authority,
        database_gate=database_gate,
        local_machine=machine_identity.name(),
    )
    pin_agent(None)

    def snapshot(
        _agent_id: int, *, database: Database
    ) -> tuple[BaseAgentState, dict[str, Any], None]:
        assert isinstance(database, Database)
        return BaseAgentState(), {}, None

    monkeypatch.setattr(plugin_state_codec, "load_snapshot", snapshot)
    with impersonation.attach(lease["id"]):
        meiva.self.pause_heartbeat(1800)
    assert lease["agent_id"] not in selected(pool)
    assert db_conn.execute(
        "SELECT duration_s FROM heartbeat_pause_log WHERE agent_id=%s", (lease["agent_id"],)
    ).fetchone() == (1800,)


def test_heartbeat_history_survives_ack_including_activation_backlog(
    pool: ConnectionPool,
    db_conn: psycopg.Connection,
    database: Database,
    event_bus: EventBus,
    config_authority: ConfigAuthority,
    *,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
):
    owner = create_native_owner(db_conn, local_machine=machine_identity.name())
    send_heartbeat_checkin(pool, database, event_bus, owner.agent_id, 7)
    lease = accept_controller(
        owner,
        authority=config_authority,
        database_gate=database_gate,
        local_machine=machine_identity.name(),
    )
    send_heartbeat_checkin(pool, database, event_bus, owner.agent_id, 8)
    messages = leases.inbox(database, lease["id"], local_machine=machine_identity.name())
    ids = [m["id"] for m in messages]
    assert len(ids) == 2
    assert all(m["kind"] == "heartbeat" for m in messages)
    leases.ack(
        database,
        event_bus,
        lease["id"],
        ids,
        local_machine=machine_identity.name(),
    )
    assert leases.inbox(database, lease["id"], local_machine=machine_identity.name()) == []
    records = db_conn.execute(
        "SELECT (payload->>'inbound_id')::bigint FROM agent_impersonation_entries "
        "WHERE lease_id=%s AND kind='message' AND payload->>'kind'='heartbeat' ORDER BY seq",
        (lease["id"],),
    ).fetchall()
    assert [r[0] for r in records] == ids


# meiva:note
# Make the durable check-in interval due; the idle clock is already aged.
def _age_heartbeat(db_conn: psycopg.Connection, agent_id: int) -> None:
    db_conn.execute(
        "UPDATE agents_meta SET last_heartbeat_at = now() - interval '400 seconds' WHERE id = %s",
        (agent_id,),
    )
    db_conn.commit()


def _set_marker(db_conn: psycopg.Connection, agent_id: int, *, seconds_ago: float | None) -> None:
    if seconds_ago is None:
        db_conn.execute(
            "UPDATE agents_meta SET last_turn_fatal_at = NULL WHERE id = %s", (agent_id,)
        )
    else:
        db_conn.execute(
            "UPDATE agents_meta SET last_turn_fatal_at = now() - make_interval(secs => %s) "
            "WHERE id = %s",
            (seconds_ago, agent_id),
        )
    db_conn.commit()


# meiva:note
# The narrow exception is a conjunction: idling, crash-marked, still owing a receipt, and
# inside the reap grace. Every partner of that conjunction is a separate row here, so a
# future widening shows up as a failure instead of a silent extra wake.
@pytest.mark.usefixtures("event_pipeline")
def test_the_handoff_recheck_selects_only_the_crash_marked_row_that_owes_a_receipt(
    redis_url: str,
    pool: ConnectionPool,
    db_conn: psycopg.Connection,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    *,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    def _idle(*, status: str = "idling") -> int:
        agent_id = make_idle(
            db_conn,
            status_changed_s_ago=900,
            last_active_s_ago=900,
            status=status,
            model_catalog=model_catalog,
            config_authority=config_authority,
            database_gate=database_gate,
            local_machine=machine_identity,
        )
        _age_heartbeat(db_conn, agent_id)
        return agent_id

    owed = _idle()
    lease_row(db_conn, owed, status="released", local_machine=machine_identity)
    _set_marker(db_conn, owed, seconds_ago=60)

    marker_only = _idle()
    _set_marker(db_conn, marker_only, seconds_ago=60)

    owed_without_marker = _idle()
    lease_row(db_conn, owed_without_marker, status="released", local_machine=machine_identity)

    owed_but_running = _idle(status="running")
    lease_row(db_conn, owed_but_running, status="released", local_machine=machine_identity)
    _set_marker(db_conn, owed_but_running, seconds_ago=60)

    reasons = selected_with_reason(pool)
    assert reasons[owed] == "unapplied_handoff_receipt"
    # meiva:note
    # Three negatives around that positive: a crash marker alone is the ordinary cadence, an
    # owed receipt alone stays excluded because nothing says the row is dead, and the
    # exception is idling-only because a running row has a live invocation.
    assert reasons[marker_only] is None
    assert owed_without_marker not in reasons
    assert owed_but_running not in reasons


# meiva:note
# The two boundaries of the narrow exception: the reap grace it is scoped to, and the pause
# window it still respects. Past the grace the reaper owns the row, and a paused agent asked
# not to be woken stays silent even when it owes a receipt.
@pytest.mark.usefixtures("event_pipeline")
def test_the_handoff_recheck_ends_with_the_grace_and_respects_a_pause(
    redis_url: str,
    pool: ConnectionPool,
    db_conn: psycopg.Connection,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    *,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    def _owed(*, marker_s_ago: float) -> int:
        agent_id = make_idle(
            db_conn,
            status_changed_s_ago=900,
            last_active_s_ago=900,
            model_catalog=model_catalog,
            config_authority=config_authority,
            database_gate=database_gate,
            local_machine=machine_identity,
        )
        lease_row(db_conn, agent_id, status="released", local_machine=machine_identity)
        _set_marker(db_conn, agent_id, seconds_ago=marker_s_ago)
        _age_heartbeat(db_conn, agent_id)
        return agent_id

    stale = _owed(marker_s_ago=1200)
    fresh = _owed(marker_s_ago=60)

    assert stale not in selected_with_reason(pool)
    assert selected_with_reason(pool)[fresh] == "unapplied_handoff_receipt"
    # meiva:note
    # The same row past the grace is the exception again only while the grace covers it.
    assert selected_with_reason(pool, corpse_grace_s=1800.0)[stale] == "unapplied_handoff_receipt"

    db_conn.execute(
        "UPDATE agents_meta SET heartbeat_paused_until = now() + interval '1 hour' WHERE id = %s",
        (fresh,),
    )
    db_conn.commit()
    assert fresh not in selected_with_reason(pool)
    db_conn.execute(
        "UPDATE agents_meta SET heartbeat_paused_until = now() - interval '1 second' WHERE id = %s",
        (fresh,),
    )
    db_conn.commit()
    assert selected_with_reason(pool)[fresh] == "unapplied_handoff_receipt"

"""Completion digest delivery, a loop of the heartbeat service."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import psycopg
import pytest
from psycopg import sql

from base.clock import Clock, ClockConfig
from base.clock.tests.manual_timers import ManualTimers
from base.clock.tests.seeded_rng import seeded_rng
from base.cluster.machine import MachineIdentityReader
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.daemon.schedules.completion_notices import (
    CompletionNotice,
    record_hourly_notice,
)
from base.db import Database, create_agent
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from ops.cluster.rpc import RpcClient
from services.wake.heartbeat import completion_digest


def _agent(db_conn: psycopg.Connection) -> int:
    agent_id = create_agent(db_conn)
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agents_meta (id, spawner, status) VALUES (%s, 'test', 'idling')",
            (agent_id,),
        )
    db_conn.commit()
    return agent_id


def test_flush_once_delivers_one_digest_and_marks_the_authoritative_events(
    redis_url: str,
    db_conn: psycopg.Connection,
    *,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentityReader,
    rpc: RpcClient,
) -> None:
    import base.db

    agent_id = _agent(db_conn)
    record_hourly_notice(
        db_conn,
        agent_id,
        CompletionNotice(
            source="shell:70",
            content="Background command 'ok' finished. Full output at ok.log.",
        ),
    )
    record_hourly_notice(
        db_conn,
        agent_id,
        CompletionNotice(
            source="shell:71",
            content="Background command 'failed' finished. Full output at bad.log.",
        ),
    )
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE completion_notice_events SET created_at = %s",
            (datetime(2026, 9, 22, 10, tzinfo=UTC),),
        )
    db_conn.commit()

    pool = base.db.pool(max_size=2, gate=database_gate)
    try:
        assert (
            asyncio.run(
                completion_digest.flush_once(
                    pool,
                    Database.from_settings(gate=database_gate),
                    EventBus.from_settings(rng=seeded_rng()),
                    now=datetime(2026, 9, 22, 12, tzinfo=UTC),
                    machine=machine_identity,
                    rpc=rpc,
                )
            )
            == 1
        )
    finally:
        pool.close()

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT count(*), count(digest_inbound_id) FROM completion_notice_events "
            "WHERE agent_id = %s",
            (agent_id,),
        )
        assert cur.fetchone() == (2, 2)
        cur.execute(
            "SELECT content, source FROM inbound_messages WHERE agent_id = %s",
            (agent_id,),
        )
        digest = cur.fetchone()
    assert digest is not None
    assert digest[1] == "system:completion-digest"
    assert "2 completion notices" in digest[0]

    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE completion_notice_events SET created_at = %s WHERE agent_id = %s",
            (datetime(2026, 9, 14, 10, tzinfo=UTC), agent_id),
        )
    db_conn.commit()
    pool = base.db.pool(max_size=2, gate=database_gate)
    try:
        assert (
            asyncio.run(
                completion_digest.flush_once(
                    pool,
                    Database.from_settings(gate=database_gate),
                    EventBus.from_settings(rng=seeded_rng()),
                    now=datetime(2026, 9, 22, 12, tzinfo=UTC),
                    machine=machine_identity,
                    rpc=rpc,
                )
            )
            == 0
        )
    finally:
        pool.close()
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM completion_notice_events WHERE agent_id = %s", (agent_id,)
        )
        assert cur.fetchone() == (0,)


def test_unknown_digest_failure_propagates_to_service(
    db_conn: psycopg.Connection,
    *,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentityReader,
    rpc: RpcClient,
) -> None:
    """An unknown failure ends this round rather than deferring poison work."""
    import base.db

    blocked_agent = _agent(db_conn)
    delivered_agent = _agent(db_conn)
    for agent_id in (blocked_agent, delivered_agent):
        record_hourly_notice(
            db_conn,
            agent_id,
            CompletionNotice(
                source=f"shell:{agent_id}",
                content="Background command 'ok' finished. Full output at ok.log.",
            ),
        )
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE completion_notice_events SET created_at = %s",
            (datetime(2026, 9, 22, 10, tzinfo=UTC),),
        )
    db_conn.commit()

    pool = base.db.pool(max_size=2, gate=database_gate)
    try:
        db_conn.execute(
            "CREATE FUNCTION test_digest_poison() RETURNS trigger LANGUAGE plpgsql "
            "AS $$ BEGIN RAISE EXCEPTION 'poison digest'; END $$"
        )
        db_conn.execute(
            sql.SQL(
                "CREATE TRIGGER test_digest_poison BEFORE INSERT ON inbound_messages "
                "FOR EACH ROW WHEN (NEW.agent_id = {}) EXECUTE FUNCTION test_digest_poison()"
            ).format(sql.Literal(blocked_agent))
        )
        db_conn.commit()
        with pytest.raises(psycopg.errors.RaiseException, match="poison digest"):
            asyncio.run(
                completion_digest.flush_once(
                    pool,
                    Database.from_settings(gate=database_gate),
                    EventBus.from_settings(rng=seeded_rng()),
                    now=datetime(2026, 9, 22, 12, tzinfo=UTC),
                    machine=machine_identity,
                    rpc=rpc,
                )
            )
    finally:
        pool.close()
        db_conn.rollback()
        db_conn.execute("DROP TRIGGER IF EXISTS test_digest_poison ON inbound_messages")
        db_conn.execute("DROP FUNCTION IF EXISTS test_digest_poison()")
        db_conn.commit()

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT agent_id, digest_inbound_id IS NOT NULL FROM completion_notice_events "
            "ORDER BY agent_id"
        )
        assert cur.fetchall() == [(blocked_agent, False), (delivered_agent, False)]


def test_digest_postcommit_failure_keeps_events_unmarked_and_recovers_same_inbound(
    redis_url: str,
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
    *,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentityReader,
    rpc: RpcClient,
) -> None:
    import base.db
    from base.agents.inbox.chat_delivery import ChatInboundCommittedError
    from ops import lifecycle

    agent_id = _agent(db_conn)
    record_hourly_notice(
        db_conn,
        agent_id,
        CompletionNotice(source="shell:receipt", content="Background command finished."),
    )
    db_conn.execute(
        "UPDATE completion_notice_events SET created_at=%s",
        (datetime(2026, 9, 22, 10, tzinfo=UTC),),
    )
    db_conn.commit()
    bug = AttributeError("digest live bug")

    async def fail_publish(*_args: object, **_kwargs: object) -> None:
        raise bug

    with base.db.pool(max_size=2, gate=database_gate) as pool:
        with monkeypatch.context() as patch:
            patch.setattr(lifecycle, "publish_inbound_arrived", fail_publish)
            with pytest.raises(ChatInboundCommittedError) as failed:
                asyncio.run(
                    completion_digest.flush_once(
                        pool,
                        Database.from_settings(gate=database_gate),
                        EventBus.from_settings(rng=seeded_rng()),
                        now=datetime(2026, 9, 22, 12, tzinfo=UTC),
                        machine=machine_identity,
                        rpc=rpc,
                    )
                )
            assert failed.value.__cause__ is bug
            assert db_conn.execute(
                "SELECT digest_inbound_id FROM completion_notice_events WHERE agent_id=%s",
                (agent_id,),
            ).fetchone() == (None,)
        assert (
            asyncio.run(
                completion_digest.flush_once(
                    pool,
                    Database.from_settings(gate=database_gate),
                    EventBus.from_settings(rng=seeded_rng()),
                    now=datetime(2026, 9, 22, 12, tzinfo=UTC),
                    machine=machine_identity,
                    rpc=rpc,
                )
            )
            == 1
        )
    assert db_conn.execute(
        "SELECT digest_inbound_id FROM completion_notice_events WHERE agent_id=%s",
        (agent_id,),
    ).fetchone() == (failed.value.receipt.inbound_id,)
    assert db_conn.execute(
        "SELECT count(*) FROM inbound_messages WHERE agent_id=%s",
        (agent_id,),
    ).fetchone() == (1,)


def test_owned_digest_round_uses_its_clock_to_close_each_window_once(
    redis_url: str,
    db_conn: psycopg.Connection,
    *,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentityReader,
    rpc: RpcClient,
) -> None:
    import base.db

    agent_id = _agent(db_conn)
    record_hourly_notice(
        db_conn, agent_id, CompletionNotice(source="shell:clock", content="Command finished.")
    )
    db_conn.execute(
        "UPDATE completion_notice_events SET created_at=%s WHERE agent_id=%s",
        (datetime(2026, 9, 22, 10, 30, tzinfo=UTC), agent_id),
    )
    db_conn.commit()
    manual = ManualTimers(wall_start=datetime(2026, 9, 22, 10, 45, tzinfo=UTC))
    clock = Clock(ClockConfig("UTC", "UTC", False), timers=manual, now=manual.now)
    with base.db.pool(max_size=2, gate=database_gate) as pool:
        digest = completion_digest.CompletionDigestRound(
            pool,
            Database.from_settings(gate=database_gate),
            EventBus.from_settings(rng=seeded_rng()),
            machine_identity,
            clock,
            completion_digest.DigestConfig(),
            rpc=rpc,
        )
        asyncio.run(digest.run_round())
        assert db_conn.execute(
            "SELECT count(*) FROM inbound_messages WHERE agent_id=%s", (agent_id,)
        ).fetchone() == (0,)
        manual.advance(15 * 60)
        asyncio.run(digest.run_round())
        asyncio.run(digest.run_round())
    assert db_conn.execute(
        "SELECT count(*), count(digest_inbound_id) FROM completion_notice_events WHERE agent_id=%s",
        (agent_id,),
    ).fetchone() == (1, 1)
    assert db_conn.execute(
        "SELECT count(*) FROM inbound_messages WHERE agent_id=%s AND source='system:completion-digest'",
        (agent_id,),
    ).fetchone() == (1,)

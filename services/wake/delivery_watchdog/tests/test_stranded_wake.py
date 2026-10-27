# meiva:note
# `services.wake.delivery_watchdog.stranded_wake` — the delivery fallback's scan.
# `select_stranded` answers one question: does a terminated owner still hold work
# its dying incarnation never claimed? `claim_stranded` is the throttle: one attempt
# per owner per cooldown, durable across a daemon restart.

from __future__ import annotations

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from base.agents.inbox.insert import insert_inbound_message
from base.agents.messages.inbound import InboundKind
from base.clock.tests.seeded_rng import seeded_rng
from base.cluster.machine import MachineIdentityReader
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.config import settings
from base.config.service_read import ConfigAuthority
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.lm.catalog import ModelCatalog
from services.wake.delivery_watchdog.stranded_wake import (
    STRANDED_WAKE_REASON,
    StrandedOwner,
    claim_stranded,
    select_stranded,
    stranded_wake_round,
)

_AGE_BOUND_S = 86400.0
_COOLDOWN_S = 300.0

pytestmark = pytest.mark.usefixtures("event_pipeline", "redis_url")


@pytest.fixture
def pool(db_url: str):
    p = ConnectionPool(settings.data_plane.db_url, min_size=1, max_size=2, open=True)
    try:
        yield p
    finally:
        p.close()


def _agent(
    db: psycopg.Connection,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    local_machine: MachineIdentityReader,
    status: str,
) -> int:
    from base.agents.tests.units import spawn_agent

    aid = spawn_agent(
        spawner="user",
        catalog=model_catalog,
        authority=config_authority,
        database_gate=database_gate,
        machine=local_machine,
    )
    with db.cursor() as cur:
        cur.execute("UPDATE agents_meta SET status = %s WHERE id = %s", (status, aid))
    db.commit()
    return aid


def _terminate_command(db: psycopg.Connection, agent_id: int) -> int:
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO inbound_messages (agent_id, content, kind, source) "
            "VALUES (%s, '', 'terminate', 'agent:1') RETURNING id",
            (agent_id,),
        )
        row = cur.fetchone()
    db.commit()
    assert row is not None
    return int(row[0])


def _chat(
    db: psycopg.Connection,
    agent_id: int,
    *,
    database_gate: ProcessDbGate,
    source: str = "agent:1",
    age_s: float = 1.0,
) -> int:
    iid = insert_inbound_message(
        db,
        agent_id,
        "queued behind the death",
        source=source,
        bus=EventBus.from_settings(rng=seeded_rng()),
        database=Database.from_settings(gate=database_gate),
    )
    if age_s:
        with db.cursor() as cur:
            cur.execute(
                "UPDATE inbound_messages SET created_at = now() - make_interval(secs => %s) "
                "WHERE id = %s",
                (age_s, iid),
            )
    db.commit()
    return iid


def _dead_with_stranded_work(
    db: psycopg.Connection,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    local_machine: MachineIdentityReader,
) -> tuple[int, int, int]:
    # meiva:note
    # The reported shape: the kill command, then a chat, then the death.
    aid = _agent(
        db,
        model_catalog=model_catalog,
        config_authority=config_authority,
        database_gate=database_gate,
        local_machine=local_machine,
        status="running",
    )
    command = _terminate_command(db, aid)
    trigger = _chat(db, aid, database_gate=database_gate)
    with db.cursor() as cur:
        cur.execute(
            "UPDATE agents_meta SET status = 'terminated', termination_source = 'user' "
            "WHERE id = %s",
            (aid,),
        )
    db.commit()
    return aid, command, trigger


class TestSelectStranded:
    def test_picks_the_owner_and_its_oldest_stranded_row(
        self,
        db_conn: psycopg.Connection,
        pool: ConnectionPool,
        *,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
        database_gate: ProcessDbGate,
        machine_identity: MachineIdentityReader,
    ) -> None:
        aid, _command, trigger = _dead_with_stranded_work(
            db_conn,
            model_catalog=model_catalog,
            config_authority=config_authority,
            database_gate=database_gate,
            local_machine=machine_identity,
        )
        second = _chat(db_conn, aid, database_gate=database_gate, age_s=0.5)

        assert select_stranded(pool, _AGE_BOUND_S) == [
            StrandedOwner(aid, trigger, InboundKind.CHAT),
        ]
        assert second > trigger

    def test_ignores_a_terminated_owner_without_pending_work(
        self,
        db_conn: psycopg.Connection,
        pool: ConnectionPool,
        *,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
        database_gate: ProcessDbGate,
        machine_identity: MachineIdentityReader,
    ) -> None:
        aid = _agent(
            db_conn,
            model_catalog=model_catalog,
            config_authority=config_authority,
            database_gate=database_gate,
            local_machine=machine_identity,
            status="terminated",
        )
        _terminate_command(db_conn, aid)
        assert select_stranded(pool, _AGE_BOUND_S) == []

    def test_ignores_a_live_owner(
        self,
        db_conn: psycopg.Connection,
        pool: ConnectionPool,
        *,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
        database_gate: ProcessDbGate,
        machine_identity: MachineIdentityReader,
    ) -> None:
        aid = _agent(
            db_conn,
            model_catalog=model_catalog,
            config_authority=config_authority,
            database_gate=database_gate,
            local_machine=machine_identity,
            status="idling",
        )
        _terminate_command(db_conn, aid)
        _chat(db_conn, aid, database_gate=database_gate)
        assert select_stranded(pool, _AGE_BOUND_S) == []

    def test_ignores_work_queued_before_the_kill_command(
        self,
        db_conn: psycopg.Connection,
        pool: ConnectionPool,
        *,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
        database_gate: ProcessDbGate,
        machine_identity: MachineIdentityReader,
    ) -> None:
        # meiva:note
        # The owner (or its killer) decided to stop; older queued work cannot
        # reverse that decision.
        aid = _agent(
            db_conn,
            model_catalog=model_catalog,
            config_authority=config_authority,
            database_gate=database_gate,
            local_machine=machine_identity,
            status="running",
        )
        _chat(db_conn, aid, database_gate=database_gate)
        _terminate_command(db_conn, aid)
        with db_conn.cursor() as cur:
            cur.execute("UPDATE agents_meta SET status = 'terminated' WHERE id = %s", (aid,))
        db_conn.commit()
        assert select_stranded(pool, _AGE_BOUND_S) == []

    def test_ignores_leftover_work_after_a_commanded_life_died_involuntarily(
        self,
        db_conn: psycopg.Connection,
        pool: ConnectionPool,
        *,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
        database_gate: ProcessDbGate,
        machine_identity: MachineIdentityReader,
    ) -> None:
        # meiva:note
        # Cross-life replay is the shape this fence exists to refuse: life 1 was
        # ended by a command, the agent came back, life 2 left a chat unclaimed and
        # then died involuntarily. That death's mark — not the old life's command —
        # decides, so the row stays stranded for the next message or the dead letter.
        aid = _agent(
            db_conn,
            model_catalog=model_catalog,
            config_authority=config_authority,
            database_gate=database_gate,
            local_machine=machine_identity,
            status="running",
        )
        _terminate_command(db_conn, aid)
        _chat(db_conn, aid, database_gate=database_gate, age_s=600)
        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE agents_meta SET status = 'terminated', termination_source = 'reaper' "
                "WHERE id = %s",
                (aid,),
            )
        db_conn.commit()
        assert select_stranded(pool, _AGE_BOUND_S) == []

    def test_ignores_an_involuntary_death_without_any_command(
        self,
        db_conn: psycopg.Connection,
        pool: ConnectionPool,
        *,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
        database_gate: ProcessDbGate,
        machine_identity: MachineIdentityReader,
    ) -> None:
        # meiva:note
        # A crash or a failed launch has no command behind it and no command mark:
        # only work that arrives after the death wakes that corpse.
        aid = _agent(
            db_conn,
            model_catalog=model_catalog,
            config_authority=config_authority,
            database_gate=database_gate,
            local_machine=machine_identity,
            status="running",
        )
        _chat(db_conn, aid, database_gate=database_gate, age_s=600)
        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE agents_meta SET status = 'terminated', termination_source = 'reaper' "
                "WHERE id = %s",
                (aid,),
            )
        db_conn.commit()
        assert select_stranded(pool, _AGE_BOUND_S) == []

    def test_ignores_work_queued_between_two_kill_commands(
        self,
        db_conn: psycopg.Connection,
        pool: ConnectionPool,
        *,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
        database_gate: ProcessDbGate,
        machine_identity: MachineIdentityReader,
    ) -> None:
        # meiva:note
        # A repeated force on an already terminated row changes no status but does
        # append a command; work queued before that newer intent cannot outrank it.
        aid = _agent(
            db_conn,
            model_catalog=model_catalog,
            config_authority=config_authority,
            database_gate=database_gate,
            local_machine=machine_identity,
            status="running",
        )
        _terminate_command(db_conn, aid)
        _chat(db_conn, aid, database_gate=database_gate)
        _terminate_command(db_conn, aid)
        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE agents_meta SET status = 'terminated', termination_source = 'user' "
                "WHERE id = %s",
                (aid,),
            )
        db_conn.commit()
        assert select_stranded(pool, _AGE_BOUND_S) == []

    def test_ignores_a_system_notice(
        self,
        db_conn: psycopg.Connection,
        pool: ConnectionPool,
        *,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
        database_gate: ProcessDbGate,
        machine_identity: MachineIdentityReader,
    ) -> None:
        # meiva:note
        # A notice queued for a dead owner is read on a later life and never wakes
        # one (user ruling 2026-08-27).
        aid, _command, _trigger = _dead_with_stranded_work(
            db_conn,
            model_catalog=model_catalog,
            config_authority=config_authority,
            database_gate=database_gate,
            local_machine=machine_identity,
        )
        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE inbound_messages SET status = 'done' WHERE agent_id = %s AND kind = 'chat'",
                (aid,),
            )
        db_conn.commit()
        _chat(db_conn, aid, database_gate=database_gate, source="system:notice-expire")
        assert select_stranded(pool, _AGE_BOUND_S) == []

    def test_ignores_work_past_the_age_bound(
        self,
        db_conn: psycopg.Connection,
        pool: ConnectionPool,
        *,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
        database_gate: ProcessDbGate,
        machine_identity: MachineIdentityReader,
    ) -> None:
        # meiva:note
        # Past the dead-letter bound the row is retired, not woken for.
        aid, _command, _trigger = _dead_with_stranded_work(
            db_conn,
            model_catalog=model_catalog,
            config_authority=config_authority,
            database_gate=database_gate,
            local_machine=machine_identity,
        )
        assert select_stranded(pool, 0.0) == []
        assert [o.agent_id for o in select_stranded(pool, _AGE_BOUND_S)] == [aid]

    def test_ignores_a_haltered_or_suppressed_owner(
        self,
        db_conn: psycopg.Connection,
        pool: ConnectionPool,
        *,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
        database_gate: ProcessDbGate,
        machine_identity: MachineIdentityReader,
    ) -> None:
        # meiva:note
        # An until-human halt is not resumed by a timer, and the cooldown window this
        # loop stamps is the same gate.
        aid, _command, _trigger = _dead_with_stranded_work(
            db_conn,
            model_catalog=model_catalog,
            config_authority=config_authority,
            database_gate=database_gate,
            local_machine=machine_identity,
        )
        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE agents_meta SET wake_suppressed_until = now() + interval '5 minutes' "
                "WHERE id = %s",
                (aid,),
            )
        db_conn.commit()
        assert select_stranded(pool, _AGE_BOUND_S) == []

        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE agents_meta SET wake_suppressed_until = NULL, permanent_reject_streak = 1 "
                "WHERE id = %s",
                (aid,),
            )
        db_conn.commit()
        assert select_stranded(pool, _AGE_BOUND_S) == []

    def test_a_breaker_window_does_not_suppress_a_stranded_wake_it_did_not_earn(
        self,
        db_conn: psycopg.Connection,
        pool: ConnectionPool,
        *,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
        database_gate: ProcessDbGate,
        machine_identity: MachineIdentityReader,
    ) -> None:
        # meiva:note
        # The residue #5667 removes: the streak cleared, the breaker's "until human" window
        # stayed on the shared column. The halt is the breaker's business — the breaker
        # clause above already says it is clear — so this loop's own cooldown is the only
        # window that may hold it back.
        aid, _command, _trigger = _dead_with_stranded_work(
            db_conn,
            model_catalog=model_catalog,
            config_authority=config_authority,
            database_gate=database_gate,
            local_machine=machine_identity,
        )
        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE agents_meta SET permanent_reject_streak = 0, "
                "wake_suppressed_until = now() + interval '10 years', "
                "wake_suppress_reason = 'permanent_provider_reject' WHERE id = %s",
                (aid,),
            )
        db_conn.commit()
        assert [o.agent_id for o in select_stranded(pool, _AGE_BOUND_S)] == [aid]

        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE agents_meta SET wake_suppressed_until = now() + interval '5 minutes', "
                "wake_suppress_reason = 'stranded_wake' WHERE id = %s",
                (aid,),
            )
        db_conn.commit()
        assert select_stranded(pool, _AGE_BOUND_S) == []


class TestClaimStranded:
    def test_one_attempt_per_cooldown(
        self,
        db_conn: psycopg.Connection,
        pool: ConnectionPool,
        *,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
        database_gate: ProcessDbGate,
        machine_identity: MachineIdentityReader,
    ) -> None:
        aid, _command, _trigger = _dead_with_stranded_work(
            db_conn,
            model_catalog=model_catalog,
            config_authority=config_authority,
            database_gate=database_gate,
            local_machine=machine_identity,
        )
        assert claim_stranded(pool, [aid], _COOLDOWN_S, 10) == [aid]
        # meiva:note
        # The stamped window is the throttle: the same owner is not claimed again.
        assert claim_stranded(pool, [aid], _COOLDOWN_S, 10) == []
        assert select_stranded(pool, _AGE_BOUND_S) == []

        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT wake_suppress_reason FROM agents_meta WHERE id = %s",
                (aid,),
            )
            row = cur.fetchone()
        assert row == (STRANDED_WAKE_REASON,)

    def test_caps_the_round_at_max_per_round(
        self,
        db_conn: psycopg.Connection,
        pool: ConnectionPool,
        *,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
        database_gate: ProcessDbGate,
        machine_identity: MachineIdentityReader,
    ) -> None:
        owners = [
            _dead_with_stranded_work(
                db_conn,
                model_catalog=model_catalog,
                config_authority=config_authority,
                database_gate=database_gate,
                local_machine=machine_identity,
            )[0]
            for _ in range(3)
        ]
        claimed = claim_stranded(pool, owners, _COOLDOWN_S, 2)
        assert len(claimed) == 2
        assert claim_stranded(pool, owners, _COOLDOWN_S, 2) == [max(owners)]

    def test_never_overwrites_the_until_human_halt(
        self,
        db_conn: psycopg.Connection,
        pool: ConnectionPool,
        *,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
        database_gate: ProcessDbGate,
        machine_identity: MachineIdentityReader,
    ) -> None:
        aid, _command, _trigger = _dead_with_stranded_work(
            db_conn,
            model_catalog=model_catalog,
            config_authority=config_authority,
            database_gate=database_gate,
            local_machine=machine_identity,
        )
        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE agents_meta SET wake_suppressed_until = now() + interval '10 years', "
                "wake_suppress_reason = 'permanent_provider_reject' WHERE id = %s",
                (aid,),
            )
        db_conn.commit()
        assert claim_stranded(pool, [aid], _COOLDOWN_S, 10) == []


class TestStrandedWakeRound:
    async def test_wakes_each_owner_once(
        self,
        db_conn: psycopg.Connection,
        pool: ConnectionPool,
        database: Database,
        event_bus: EventBus,
        *,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
        database_gate: ProcessDbGate,
        machine_identity: MachineIdentityReader,
    ) -> None:
        aid, _command, trigger = _dead_with_stranded_work(
            db_conn,
            model_catalog=model_catalog,
            config_authority=config_authority,
            database_gate=database_gate,
            local_machine=machine_identity,
        )
        woken: list[StrandedOwner] = []

        async def fake_wake(
            _db: Database,
            _bus: EventBus,
            owner: StrandedOwner,
            _local_machine: str,
        ) -> bool:
            woken.append(owner)
            return True

        first = await stranded_wake_round(
            pool,
            database,
            event_bus,
            machine_identity.name(),
            age_bound_s=_AGE_BOUND_S,
            cooldown_s=_COOLDOWN_S,
            max_per_round=2,
            wake=fake_wake,
        )
        second = await stranded_wake_round(
            pool,
            database,
            event_bus,
            machine_identity.name(),
            age_bound_s=_AGE_BOUND_S,
            cooldown_s=_COOLDOWN_S,
            max_per_round=2,
            wake=fake_wake,
        )

        assert (first, second) == (1, 0)
        assert woken == [StrandedOwner(aid, trigger, InboundKind.CHAT)]

from __future__ import annotations

import time

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from base.cluster.machine import MachineIdentityReader
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.config.service_read import ConfigAuthority
from base.db.code_version_gate import ProcessDbGate
from base.lm.catalog import ModelCatalog
from services.wake.heartbeat.selection import select_idle_agents_needing_heartbeat
from services.wake.heartbeat.tests.heartbeat_support import (
    BACKOFF_MAX_INTERVAL_S,
    THRESHOLD_S,
    lease,
    make_idle,
)
from services.wake.heartbeat.tests.heartbeat_support import pool as pool


# meiva:note
# agent_id -> idle_minutes, dropping the reason these assertions do not read.
def _by_agent(rows: list[tuple[int, float, str | None]]) -> dict[int, float]:
    return {agent_id: idle for agent_id, idle, _reason in rows}


def _selected(pool: ConnectionPool) -> dict[int, float]:
    return _by_agent(
        select_idle_agents_needing_heartbeat(
            pool, THRESHOLD_S, backoff_max_interval_s=BACKOFF_MAX_INTERVAL_S, now=time.time()
        )
    )


class TestSelectIdleAgents:
    def test_idle_past_threshold_unpaused_is_selected(
        self,
        redis_url: str,
        pool: ConnectionPool,
        db_conn: psycopg.Connection,
        *,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
        database_gate: ProcessDbGate,
        machine_identity: MachineIdentityReader,
    ) -> None:
        aid = make_idle(
            db_conn,
            status_changed_s_ago=400,
            model_catalog=model_catalog,
            config_authority=config_authority,
            database_gate=database_gate,
            local_machine=machine_identity,
        )
        selected = _selected(pool)
        assert aid in selected
        assert float(selected[aid]) == pytest.approx(400 / 60.0, abs=0.5)

    def test_idle_under_threshold_unpaused_excluded(
        self,
        redis_url: str,
        pool: ConnectionPool,
        db_conn: psycopg.Connection,
        *,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
        database_gate: ProcessDbGate,
        machine_identity: MachineIdentityReader,
    ) -> None:
        aid = make_idle(
            db_conn,
            status_changed_s_ago=120,
            model_catalog=model_catalog,
            config_authority=config_authority,
            database_gate=database_gate,
            local_machine=machine_identity,
        )
        assert aid not in _selected(pool)

    def test_running_agent_excluded(
        self,
        redis_url: str,
        pool: ConnectionPool,
        db_conn: psycopg.Connection,
        *,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
        database_gate: ProcessDbGate,
        machine_identity: MachineIdentityReader,
    ) -> None:
        aid = make_idle(
            db_conn,
            status_changed_s_ago=400,
            status="running",
            model_catalog=model_catalog,
            config_authority=config_authority,
            database_gate=database_gate,
            local_machine=machine_identity,
        )
        assert aid not in _selected(pool)

    def test_pending_inbound_excluded(
        self,
        redis_url: str,
        pool: ConnectionPool,
        db_conn: psycopg.Connection,
        *,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
        database_gate: ProcessDbGate,
        machine_identity: MachineIdentityReader,
    ) -> None:
        aid = make_idle(
            db_conn,
            status_changed_s_ago=400,
            model_catalog=model_catalog,
            config_authority=config_authority,
            database_gate=database_gate,
            local_machine=machine_identity,
        )
        with db_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO inbound_messages (agent_id, content, kind, source) "
                "VALUES (%s, 'hi', 'chat', 'user')",
                (aid,),
            )
        db_conn.commit()
        assert aid not in _selected(pool)

    @pytest.mark.parametrize("status", ["requested", "accepted"])
    def test_agent_under_open_impersonation_lease_excluded(
        self,
        redis_url: str,
        pool: ConnectionPool,
        db_conn: psycopg.Connection,
        status: str,
        *,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
        database_gate: ProcessDbGate,
        machine_identity: MachineIdentityReader,
    ) -> None:
        aid = make_idle(
            db_conn,
            status_changed_s_ago=400,
            model_catalog=model_catalog,
            config_authority=config_authority,
            database_gate=database_gate,
            local_machine=machine_identity,
        )
        lease(db_conn, aid, status=status, local_machine=machine_identity)
        assert aid not in _selected(pool)

    def test_terminal_impersonation_lease_does_not_block_checkin(
        self,
        redis_url: str,
        pool: ConnectionPool,
        db_conn: psycopg.Connection,
        *,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
        database_gate: ProcessDbGate,
        machine_identity: MachineIdentityReader,
    ) -> None:
        aid = make_idle(
            db_conn,
            status_changed_s_ago=400,
            model_catalog=model_catalog,
            config_authority=config_authority,
            database_gate=database_gate,
            local_machine=machine_identity,
        )
        lease(db_conn, aid, status="expired", handoff_applied=True, local_machine=machine_identity)
        assert aid in _selected(pool)

    def test_unapplied_impersonation_delta_excludes_agent(
        self,
        redis_url: str,
        pool: ConnectionPool,
        db_conn: psycopg.Connection,
        *,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
        database_gate: ProcessDbGate,
        machine_identity: MachineIdentityReader,
    ) -> None:
        aid = make_idle(
            db_conn,
            status_changed_s_ago=400,
            model_catalog=model_catalog,
            config_authority=config_authority,
            database_gate=database_gate,
            local_machine=machine_identity,
        )
        lease(
            db_conn,
            aid,
            status="expired",
            delta_version=3,
            applied_version=2,
            local_machine=machine_identity,
        )
        assert aid not in _selected(pool)

    def test_unapplied_handoff_excludes_agent(
        self,
        redis_url: str,
        pool: ConnectionPool,
        db_conn: psycopg.Connection,
        *,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
        database_gate: ProcessDbGate,
        machine_identity: MachineIdentityReader,
    ) -> None:
        aid = make_idle(
            db_conn,
            status_changed_s_ago=400,
            model_catalog=model_catalog,
            config_authority=config_authority,
            database_gate=database_gate,
            local_machine=machine_identity,
        )
        lease(db_conn, aid, status="expired", local_machine=machine_identity)
        assert aid not in _selected(pool)

    def test_applied_handoff_does_not_block_checkin(
        self,
        redis_url: str,
        pool: ConnectionPool,
        db_conn: psycopg.Connection,
        *,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
        database_gate: ProcessDbGate,
        machine_identity: MachineIdentityReader,
    ) -> None:
        aid = make_idle(
            db_conn,
            status_changed_s_ago=400,
            model_catalog=model_catalog,
            config_authority=config_authority,
            database_gate=database_gate,
            local_machine=machine_identity,
        )
        lease(
            db_conn,
            aid,
            status="expired",
            handoff_applied=True,
            local_machine=machine_identity,
        )
        assert aid in _selected(pool)

    def test_active_pause_suppresses_even_when_idle_past_threshold(
        self,
        redis_url: str,
        pool: ConnectionPool,
        db_conn: psycopg.Connection,
        *,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
        database_gate: ProcessDbGate,
        machine_identity: MachineIdentityReader,
    ) -> None:
        aid = make_idle(
            db_conn,
            status_changed_s_ago=600,
            paused_until_s_ahead=1800,
            model_catalog=model_catalog,
            config_authority=config_authority,
            database_gate=database_gate,
            local_machine=machine_identity,
        )
        assert aid not in _selected(pool)

    def test_real_turn_during_pause_delays_checkin_past_window(
        self,
        redis_url: str,
        pool: ConnectionPool,
        db_conn: psycopg.Connection,
        *,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
        database_gate: ProcessDbGate,
        machine_identity: MachineIdentityReader,
    ) -> None:
        aid = make_idle(
            db_conn,
            status_changed_s_ago=10,
            last_active_s_ago=10,
            paused_until_s_ahead=-1,
            model_catalog=model_catalog,
            config_authority=config_authority,
            database_gate=database_gate,
            local_machine=machine_identity,
        )
        assert aid not in _selected(pool)

    def test_open_pause_window_still_suppresses_checkin_after_real_turn(
        self,
        redis_url: str,
        pool: ConnectionPool,
        db_conn: psycopg.Connection,
        *,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
        database_gate: ProcessDbGate,
        machine_identity: MachineIdentityReader,
    ) -> None:
        open_window = make_idle(
            db_conn,
            status_changed_s_ago=10,
            last_active_s_ago=10,
            paused_until_s_ahead=60,
            model_catalog=model_catalog,
            config_authority=config_authority,
            database_gate=database_gate,
            local_machine=machine_identity,
        )
        expired_window = make_idle(
            db_conn,
            status_changed_s_ago=10,
            last_active_s_ago=10,
            paused_until_s_ahead=-1,
            model_catalog=model_catalog,
            config_authority=config_authority,
            database_gate=database_gate,
            local_machine=machine_identity,
        )
        selected = _selected(pool)
        assert open_window not in selected
        assert expired_window not in selected

    def test_pause_expired_before_last_wake_uses_normal_clock(
        self,
        redis_url: str,
        pool: ConnectionPool,
        db_conn: psycopg.Connection,
        *,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
        database_gate: ProcessDbGate,
        machine_identity: MachineIdentityReader,
    ) -> None:
        aid = make_idle(
            db_conn,
            status_changed_s_ago=120,
            paused_until_s_ahead=-300,
            model_catalog=model_catalog,
            config_authority=config_authority,
            database_gate=database_gate,
            local_machine=machine_identity,
        )
        assert aid not in _selected(pool)

    def test_pause_expired_after_long_idle_is_due(
        self,
        redis_url: str,
        pool: ConnectionPool,
        db_conn: psycopg.Connection,
        *,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
        database_gate: ProcessDbGate,
        machine_identity: MachineIdentityReader,
    ) -> None:
        aid = make_idle(
            db_conn,
            status_changed_s_ago=600,
            paused_until_s_ahead=-60,
            model_catalog=model_catalog,
            config_authority=config_authority,
            database_gate=database_gate,
            local_machine=machine_identity,
        )
        assert aid in _selected(pool)


class TestIdleClockCountsRealActivityOnly:
    def test_ops_restart_does_not_reset_idle_clock(
        self,
        redis_url: str,
        pool: ConnectionPool,
        db_conn: psycopg.Connection,
        *,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
        database_gate: ProcessDbGate,
        machine_identity: MachineIdentityReader,
    ) -> None:
        aid = make_idle(
            db_conn,
            status_changed_s_ago=5,
            last_active_s_ago=400,
            model_catalog=model_catalog,
            config_authority=config_authority,
            database_gate=database_gate,
            local_machine=machine_identity,
        )
        assert aid in _selected(pool)

    def test_ops_restart_does_not_make_long_idle_agent_look_fresh(
        self,
        redis_url: str,
        pool: ConnectionPool,
        db_conn: psycopg.Connection,
        *,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
        database_gate: ProcessDbGate,
        machine_identity: MachineIdentityReader,
    ) -> None:
        aid = make_idle(
            db_conn,
            status_changed_s_ago=5,
            last_active_s_ago=400,
            model_catalog=model_catalog,
            config_authority=config_authority,
            database_gate=database_gate,
            local_machine=machine_identity,
        )
        selected = _selected(pool)
        assert aid in selected
        assert float(selected[aid]) == pytest.approx(400 / 60.0, abs=0.5)

    def test_recent_real_turn_resets_idle_clock(
        self,
        redis_url: str,
        pool: ConnectionPool,
        db_conn: psycopg.Connection,
        *,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
        database_gate: ProcessDbGate,
        machine_identity: MachineIdentityReader,
    ) -> None:
        aid = make_idle(
            db_conn,
            status_changed_s_ago=600,
            last_active_s_ago=30,
            model_catalog=model_catalog,
            config_authority=config_authority,
            database_gate=database_gate,
            local_machine=machine_identity,
        )
        assert aid not in _selected(pool)


class TestWakeupStormFlattening:
    def _backdate(self, db: psycopg.Connection, aid: int, secs: float) -> None:
        with db.cursor() as cur:
            cur.execute(
                "UPDATE agents_meta SET status_changed_at = now() - make_interval(secs => %s), "
                "       last_active_at = now() - make_interval(secs => %s) "
                "WHERE id = %s",
                (secs, secs, aid),
            )
        db.commit()

    def test_jitter_offsets_due_time_by_id_mod_span(
        self,
        redis_url: str,
        pool: ConnectionPool,
        db_conn: psycopg.Connection,
        *,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
        database_gate: ProcessDbGate,
        machine_identity: MachineIdentityReader,
    ) -> None:
        span, threshold = 100.0, 300.0
        aid = make_idle(
            db_conn,
            status_changed_s_ago=1,
            model_catalog=model_catalog,
            config_authority=config_authority,
            database_gate=database_gate,
            local_machine=machine_identity,
        )
        offset = aid % int(span)

        self._backdate(db_conn, aid, threshold + offset - 5)
        got = _by_agent(
            select_idle_agents_needing_heartbeat(
                pool,
                threshold,
                jitter_span_s=span,
                backoff_max_interval_s=BACKOFF_MAX_INTERVAL_S,
                now=time.time(),
            )
        )
        assert aid not in got, "idle just short of the jittered due-time must be excluded"

        self._backdate(db_conn, aid, threshold + offset + 5)
        got = _by_agent(
            select_idle_agents_needing_heartbeat(
                pool,
                threshold,
                jitter_span_s=span,
                backoff_max_interval_s=BACKOFF_MAX_INTERVAL_S,
                now=time.time(),
            )
        )
        assert aid in got, "idle just past the jittered due-time must be selected"

    def test_zero_jitter_matches_plain_threshold(
        self,
        redis_url: str,
        pool: ConnectionPool,
        db_conn: psycopg.Connection,
        *,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
        database_gate: ProcessDbGate,
        machine_identity: MachineIdentityReader,
    ) -> None:
        aid = make_idle(
            db_conn,
            status_changed_s_ago=350,
            model_catalog=model_catalog,
            config_authority=config_authority,
            database_gate=database_gate,
            local_machine=machine_identity,
        )
        got = _by_agent(
            select_idle_agents_needing_heartbeat(
                pool,
                300.0,
                jitter_span_s=0.0,
                backoff_max_interval_s=BACKOFF_MAX_INTERVAL_S,
                now=time.time(),
            )
        )
        assert aid in got

    def test_limit_caps_batch_oldest_idle_first(
        self,
        redis_url: str,
        pool: ConnectionPool,
        db_conn: psycopg.Connection,
        *,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
        database_gate: ProcessDbGate,
        machine_identity: MachineIdentityReader,
    ) -> None:
        a_old = make_idle(
            db_conn,
            status_changed_s_ago=900,
            model_catalog=model_catalog,
            config_authority=config_authority,
            database_gate=database_gate,
            local_machine=machine_identity,
        )
        a_mid = make_idle(
            db_conn,
            status_changed_s_ago=600,
            model_catalog=model_catalog,
            config_authority=config_authority,
            database_gate=database_gate,
            local_machine=machine_identity,
        )
        a_new = make_idle(
            db_conn,
            status_changed_s_ago=400,
            model_catalog=model_catalog,
            config_authority=config_authority,
            database_gate=database_gate,
            local_machine=machine_identity,
        )

        order = [
            r[0]
            for r in select_idle_agents_needing_heartbeat(
                pool, THRESHOLD_S, backoff_max_interval_s=BACKOFF_MAX_INTERVAL_S, now=time.time()
            )
        ]
        assert order.index(a_old) < order.index(a_mid) < order.index(a_new)

        capped = select_idle_agents_needing_heartbeat(
            pool,
            THRESHOLD_S,
            limit=2,
            backoff_max_interval_s=BACKOFF_MAX_INTERVAL_S,
            now=time.time(),
        )
        assert len(capped) == 2
        assert a_new not in [r[0] for r in capped]

"""Heartbeat dispatch, emitted events, and consecutive-failure backoff."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from base import telemetry
from base.agents.tests.units import spawn_agent
from base.clock import SYSTEM_TIMERS
from base.cluster import inbound_channel
from base.cluster.machine import MachineIdentityReader
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.config import settings
from base.config.service_read import ConfigAuthority
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.events.live.redis_listener import RedisInboundListener
from base.lm.catalog import ModelCatalog
from base.paths import meiva_home
from services.wake.heartbeat.daemon import (
    backoff_deadlines,
    reconcile_checkin_outcomes,
    send_heartbeat_checkin,
)
from services.wake.heartbeat.selection import select_idle_agents_needing_heartbeat
from services.wake.heartbeat.tests.heartbeat_support import (
    BACKOFF_MAX_INTERVAL_S,
    THRESHOLD_S,
    make_idle,
)
from services.wake.heartbeat.tests.heartbeat_support import pool as pool

# meiva:note
# The listener's own budget for this wake, and a bound that only fails a wedged run: the
# verdict is `wait_one`'s bool, so the guard is never what decides a slow-but-correct wake.
_WAKE_BUDGET_S = 10.0
_WAKE_HANG_GUARD_S = _WAKE_BUDGET_S * 3
_SUBSCRIBED_POLL_S = 0.05


def _subscriber_count(bus: EventBus, channel: str) -> int:
    # meiva:note
    # How many clients Redis reports subscribed to this channel right now. redis-py types
    # `pubsub_numsub` and its reply as partially unknown, so the ignore is narrow on purpose;
    # the call itself is fully typed.
    probe = bus.sync_redis()
    try:
        return int(probe.pubsub_numsub(channel)[0][1])  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
    finally:
        probe.close()


async def _await_subscribed(bus: EventBus, channel: str) -> None:
    # meiva:note
    # Block until Redis reports a subscriber on this channel: pub/sub drops a publish that
    # lands with no subscriber, so the check-in waits for that fact instead of a fixed sleep.
    # The deadline bounds a failure; it is not the assertion.
    deadline = time.monotonic() + _WAKE_BUDGET_S
    while _subscriber_count(bus, channel) < 1:
        if time.monotonic() > deadline:
            raise AssertionError(f"no subscriber appeared on {channel} in {_WAKE_BUDGET_S}s")
        await asyncio.sleep(_SUBSCRIBED_POLL_S)


@pytest.fixture
def event_pipeline(
    database: Database, machine_identity: MachineIdentityReader, unit_home: Path
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


def _selected(pool: ConnectionPool) -> dict[int, float]:
    """agent_id -> idle_minutes for every agent the daemon would check in on."""
    return {
        agent_id: idle
        for agent_id, idle, _reason in select_idle_agents_needing_heartbeat(
            pool, THRESHOLD_S, backoff_max_interval_s=BACKOFF_MAX_INTERVAL_S, now=time.time()
        )
    }


# meiva:note
# The selected agent ids only: these assertions read membership, not idle minutes.
def _selected_ids(rows: list[tuple[int, float, str | None]]) -> set[int]:
    return {agent_id for agent_id, _idle, _reason in rows}


def _mirror_nudged(agent_id: int) -> tuple[str, str, int] | None:
    """The latest heartbeat_nudged mirror line for `agent_id` — (event_name,
    level, idle_minutes), or None while the drain thread has not landed it."""
    import json
    from datetime import UTC, datetime

    from base.paths import logs_dir

    day = datetime.now(UTC).strftime("%Y%m%d")
    path = logs_dir(home=meiva_home()) / f"events-{day}.jsonl"
    if not path.exists():
        return None
    for line in reversed(path.read_text(encoding="utf-8").splitlines()):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if obj.get("event_name") == "heartbeat_nudged" and obj.get("agent_id") == agent_id:
            return obj["event_name"], obj["level"], int(obj["attributes"]["idle_minutes"])
    return None


class TestSendHeartbeatCheckin:
    @pytest.mark.usefixtures("event_pipeline")
    def test_inserts_heartbeat_inbound_and_event(
        self,
        pool: ConnectionPool,
        db_conn: psycopg.Connection,
        database: Database,
        event_bus: EventBus,
        *,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
        database_gate: ProcessDbGate,
        machine_identity: MachineIdentityReader,
    ) -> None:
        aid = spawn_agent(
            spawner="user",
            catalog=model_catalog,
            authority=config_authority,
            database_gate=database_gate,
            machine=machine_identity,
        )
        send_heartbeat_checkin(pool, database, event_bus, aid, 7.0)
        # The emitter drains asynchronously (0.5s cadence) — flush() can
        # race the drain thread for the queue, so poll briefly for the line.
        # The PG events copy was retired at the LGTM cutover (task #1197 close-C)
        # and later dropped; the durable local copy is the JSONL mirror.
        ev = None
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            telemetry.flush()
            ev = _mirror_nudged(aid)
            if ev is not None:
                break
            time.sleep(0.05)
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT content, kind, source FROM inbound_messages WHERE agent_id = %s",
                (aid,),
            )
            inbound = cur.fetchone()
        assert inbound is not None
        content, kind, source = inbound
        assert kind == "heartbeat"
        assert source == "system"
        assert (
            content
            == "Heartbeat. Review your existing responsibilities and continue actionable work. "
            "For a known wait, pause your heartbeat; if your role is complete, end it."
        )  # idle-minutes detail lives in the event row, not the content (0064)
        assert ev is not None
        assert ev[0] == "heartbeat_nudged"
        assert ev[1] == "info"
        assert int(ev[2]) == 7

    def test_consumed_heartbeat_defers_the_next_checkin(
        self,
        pool: ConnectionPool,
        db_conn: psycopg.Connection,
        database: Database,
        event_bus: EventBus,
        *,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
        database_gate: ProcessDbGate,
        machine_identity: MachineIdentityReader,
    ) -> None:
        """A completed check-in must start a durable reminder interval.

        Regression for #5759: a permanent provider rejection leaves
        ``last_active_at`` unchanged. Once its heartbeat inbound is consumed,
        the pending-inbound guard no longer applies, so the daemon must still
        hold the agent until the configured heartbeat interval has elapsed.
        """
        aid = make_idle(
            db_conn,
            status_changed_s_ago=400,
            model_catalog=model_catalog,
            config_authority=config_authority,
            database_gate=database_gate,
            local_machine=machine_identity,
        )
        send_heartbeat_checkin(pool, database, event_bus, aid, 7.0)
        with db_conn.cursor() as cur:
            cur.execute("UPDATE inbound_messages SET status = 'done' WHERE agent_id = %s", (aid,))
        db_conn.commit()

        assert aid not in _selected(pool)

    def test_reminder_uses_heartbeat_interval_not_dispatch_step(
        self,
        pool: ConnectionPool,
        db_conn: psycopg.Connection,
        database: Database,
        event_bus: EventBus,
        *,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
        database_gate: ProcessDbGate,
        machine_identity: MachineIdentityReader,
    ) -> None:
        """The 15-second dispatcher step must not become the reminder cadence."""
        aid = make_idle(
            db_conn,
            status_changed_s_ago=400,
            model_catalog=model_catalog,
            config_authority=config_authority,
            database_gate=database_gate,
            local_machine=machine_identity,
        )
        send_heartbeat_checkin(pool, database, event_bus, aid, 7.0)
        with db_conn.cursor() as cur:
            cur.execute("UPDATE inbound_messages SET status = 'done' WHERE agent_id = %s", (aid,))
            cur.execute(
                "UPDATE agents_meta SET last_heartbeat_at = now() - interval '20 seconds' "
                "WHERE id = %s",
                (aid,),
            )
        db_conn.commit()

        assert aid not in _selected_ids(
            select_idle_agents_needing_heartbeat(
                pool,
                THRESHOLD_S,
                heartbeat_interval_s=300.0,
                backoff_max_interval_s=BACKOFF_MAX_INTERVAL_S,
                now=time.time(),
            )
        )
        assert aid in _selected_ids(
            select_idle_agents_needing_heartbeat(
                pool,
                THRESHOLD_S,
                heartbeat_interval_s=15.0,
                backoff_max_interval_s=BACKOFF_MAX_INTERVAL_S,
                now=time.time(),
            )
        )

    async def test_publishes_redis_wake_to_target(
        self,
        pool: ConnectionPool,
        db_conn: psycopg.Connection,
        database: Database,
        event_bus: EventBus,
        *,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
        database_gate: ProcessDbGate,
        machine_identity: MachineIdentityReader,
    ) -> None:
        """The check-in publishes a Redis wake to the target agent's channel so an
        idle agent runs the heartbeat turn now instead of at its next SELECT
        recheck — park a per-agent listener, fire the check-in, assert it wakes."""
        # meiva:note
        # The verdict is `wait_one`'s own answer (did a publish arrive?), never a clock
        # reading: a check-in that is slow on a loaded machine must not fail a test about the
        # wake, and a missing wake must fail it without waiting out a wall-clock bound.
        aid = spawn_agent(
            spawner="user",
            catalog=model_catalog,
            authority=config_authority,
            database_gate=database_gate,
            machine=machine_identity,
        )
        channel = inbound_channel(aid, settings.data_plane.events_channel)
        listener = RedisInboundListener(
            settings.data_plane.redis_url,
            aid,
            events_channel=settings.data_plane.events_channel,
            timers=SYSTEM_TIMERS,
        )
        try:
            wait_task = asyncio.create_task(listener.wait_one(timeout=_WAKE_BUDGET_S))
            await _await_subscribed(event_bus, channel)
            await asyncio.to_thread(send_heartbeat_checkin, pool, database, event_bus, aid, 7.0)
            woke = await asyncio.wait_for(wait_task, timeout=_WAKE_HANG_GUARD_S)
        finally:
            await listener.close()
        assert woke is True, "heartbeat check-in did not wake the parked listener"


# ───────────── consecutive-failure backoff (Task #1928) ─────────────


class TestConsecutiveFailureBackoff:
    """A check-in that produces no LLM turn is a failed check-in (the 3962
    context-overflow case: the daemon poked a permanently-rejecting agent
    ~1150 times). The daemon spaces streaking agents by `2^streak` idle
    windows; a real turn resets the streak."""

    def test_backoff_skips_agent_until_deadline(
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
        # Deadline in the future -> excluded; deadline passed -> selected.
        assert aid not in _selected_ids(
            select_idle_agents_needing_heartbeat(
                pool,
                THRESHOLD_S,
                backoff_until={aid: time.time() + 1000},
                backoff_max_interval_s=BACKOFF_MAX_INTERVAL_S,
                now=time.time(),
            )
        )
        assert aid in _selected_ids(
            select_idle_agents_needing_heartbeat(
                pool,
                THRESHOLD_S,
                backoff_until={aid: time.time() - 1},
                backoff_max_interval_s=BACKOFF_MAX_INTERVAL_S,
                now=time.time(),
            )
        )

    def test_backed_off_agent_does_not_consume_limit_slots(
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
        """The limit applies AFTER the backoff filter: a backed-off agent must
        not occupy a per-step wake-rate slot that a healthy due agent needs."""
        healthy = make_idle(
            db_conn,
            status_changed_s_ago=900,
            model_catalog=model_catalog,
            config_authority=config_authority,
            database_gate=database_gate,
            local_machine=machine_identity,
        )
        wedged = make_idle(
            db_conn,
            status_changed_s_ago=700,
            model_catalog=model_catalog,
            config_authority=config_authority,
            database_gate=database_gate,
            local_machine=machine_identity,
        )

        selected = select_idle_agents_needing_heartbeat(
            pool,
            THRESHOLD_S,
            limit=1,
            backoff_until={wedged: time.time() + 10_000},
            backoff_max_interval_s=BACKOFF_MAX_INTERVAL_S,
            now=time.time(),
        )
        assert [r[0] for r in selected] == [healthy]

    @pytest.mark.parametrize("advanced", [False, True])
    def test_real_selected_idle_clock_survives_next_checkin_cycle(
        self,
        pool: ConnectionPool,
        db_conn: psycopg.Connection,
        advanced: bool,
        database: Database,
        event_bus: EventBus,
        *,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
        database_gate: ProcessDbGate,
        machine_identity: MachineIdentityReader,
    ) -> None:
        """Use the actual PG result in pending state, as the dispatch loop does."""
        aid = make_idle(
            db_conn,
            status_changed_s_ago=1200,
            model_catalog=model_catalog,
            config_authority=config_authority,
            database_gate=database_gate,
            local_machine=machine_identity,
        )
        pending = _selected(pool)
        send_heartbeat_checkin(pool, database, event_bus, aid, pending[aid])
        if advanced:
            # Still beyond the idle threshold: recovery must use the observed
            # progress, not the independent fresh-activity shortcut.
            with db_conn.cursor() as cur:
                cur.execute(
                    "UPDATE agents_meta SET last_active_at = now() - interval '10 minutes' "
                    "WHERE id = %s",
                    (aid,),
                )
            db_conn.commit()
        streaks: dict[int, int] = {aid: 2}
        reconcile_checkin_outcomes(
            pool, pending_checkin=pending, failure_streak=streaks, idle_threshold_s=THRESHOLD_S
        )
        assert pending == {}
        assert streaks == ({} if advanced else {aid: 3})

    def test_reconcile_increments_streak_when_checkin_produced_no_turn(
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
        """The check-in was sent when the agent had been idle ~6 minutes; a
        cycle later `last_active_at` has not moved (no turn ran) -> streak 1."""
        aid = make_idle(
            db_conn,
            status_changed_s_ago=400,
            model_catalog=model_catalog,
            config_authority=config_authority,
            database_gate=database_gate,
            local_machine=machine_identity,
        )
        pending: dict[int, float] = {aid: 6.0}
        streaks: dict[int, int] = {}

        reconcile_checkin_outcomes(
            pool, pending_checkin=pending, failure_streak=streaks, idle_threshold_s=THRESHOLD_S
        )

        assert pending == {}
        assert streaks == {aid: 1}

    def test_reconcile_resets_streak_when_checkin_produced_a_turn(
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
        """The check-in produced a turn (`last_active_at` advanced) -> streak
        reset (stays empty / drops)."""
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
                "UPDATE agents_meta SET last_active_at = now() WHERE id = %s",
                (aid,),
            )
        db_conn.commit()
        pending: dict[int, float] = {aid: 6.0}
        streaks: dict[int, int] = {aid: 3}

        reconcile_checkin_outcomes(
            pool, pending_checkin=pending, failure_streak=streaks, idle_threshold_s=THRESHOLD_S
        )

        assert streaks == {}, "a turn after the check-in must clear the streak"

    def test_reconcile_resets_streak_on_fresh_activity_without_pending(
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
        """No check-in was sent this cycle (backoff active), but a real wake
        produced a turn — fresh `last_active_at` clears the streak so the
        recovered agent is probed again at the normal cadence."""
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
                "UPDATE agents_meta SET last_active_at = now() WHERE id = %s",
                (aid,),
            )
        db_conn.commit()
        streaks: dict[int, int] = {aid: 4}

        reconcile_checkin_outcomes(
            pool, pending_checkin={}, failure_streak=streaks, idle_threshold_s=THRESHOLD_S
        )

        assert streaks == {}

    def test_reconcile_stops_tracking_agents_outside_daemon_lanes(
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
            cur.execute("UPDATE agents_meta SET status = 'terminated' WHERE id = %s", (aid,))
        db_conn.commit()
        streaks: dict[int, int] = {aid: 2}

        reconcile_checkin_outcomes(
            pool, pending_checkin={}, failure_streak=streaks, idle_threshold_s=THRESHOLD_S
        )

        assert streaks == {}

    def test_backoff_deadlines_exponential_capped(self) -> None:
        """streak=1 doubles the normal interval; the growth caps at
        BACKOFF_MAX_WINDOWS so the daemon still probes a wedged agent."""
        from services.wake.heartbeat.daemon import BACKOFF_MAX_WINDOWS

        d1 = backoff_deadlines({1: 1}, THRESHOLD_S, now=time.time())
        d2 = backoff_deadlines({1: 2}, THRESHOLD_S, now=time.time())
        d10 = backoff_deadlines({1: 10}, THRESHOLD_S, now=time.time())
        assert d1[1] - time.time() == pytest.approx(2 * THRESHOLD_S, abs=1.0)  # pyright: ignore[reportUnknownMemberType]
        assert d2[1] - d1[1] == pytest.approx(2 * THRESHOLD_S, abs=1.0)  # pyright: ignore[reportUnknownMemberType]
        assert d10[1] - d2[1] == pytest.approx((BACKOFF_MAX_WINDOWS - 4) * THRESHOLD_S, abs=1.0)  # pyright: ignore[reportUnknownMemberType]


def _mirror_event(agent_id: int, event_name: str) -> dict | None:
    """The latest mirror line for (agent, event), or None while the drain
    thread has not landed it."""
    import json
    from datetime import UTC, datetime

    from base.paths import logs_dir

    day = datetime.now(UTC).strftime("%Y%m%d")
    path = logs_dir(home=meiva_home()) / f"events-{day}.jsonl"
    if not path.exists():
        return None
    for line in reversed(path.read_text(encoding="utf-8").splitlines()):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if obj.get("event_name") == event_name and obj.get("agent_id") == agent_id:
            return obj
    return None


def _poll_mirror(agent_id: int, event_name: str, timeout_s: float = 2.0) -> dict | None:
    deadline = time.monotonic() + timeout_s
    ev = None
    while time.monotonic() < deadline:
        telemetry.flush()
        ev = _mirror_event(agent_id, event_name)
        if ev is not None:
            break
        time.sleep(0.05)
    return ev

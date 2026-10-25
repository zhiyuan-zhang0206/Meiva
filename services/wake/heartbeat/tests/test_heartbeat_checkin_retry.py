import asyncio

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from base.agents.tests.units import spawn_agent
from base.clock import Clock, ClockConfig
from base.clock.tests.manual_timers import ManualTimers
from base.cluster.machine import MachineIdentityReader
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.config import settings
from base.config.service_read import ConfigAuthority
from base.daemon.loop_health import LoopProgress
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.lm.catalog import ModelCatalog
from services.wake.heartbeat import JITTER_SPAN_S
from services.wake.heartbeat import daemon as heartbeat_daemon
from services.wake.heartbeat.tests.machine_probe_setup import pool as pool


@pytest.mark.usefixtures(pool.__name__)
async def test_failed_checkin_is_retried_after_backoff_across_ticks(
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
    idle_threshold = settings.daemon.heartbeat_idle_threshold_seconds
    aid = spawn_agent(
        spawner="user",
        catalog=model_catalog,
        authority=config_authority,
        database_gate=database_gate,
        machine=machine_identity,
    )
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE agents_meta SET status = 'idling', "
            "last_active_at = now() - make_interval(secs => %s) WHERE id = %s",
            (idle_threshold + JITTER_SPAN_S + 100, aid),
        )
    db_conn.commit()

    class TickTimers(ManualTimers):
        def __init__(self) -> None:
            super().__init__()
            self.tick = -1
            self._ticks = iter([0.0, 1.0, 1.0 + idle_threshold, 2.0 + 2 * idle_threshold])

        async def asleep(self, seconds: float) -> None:
            try:
                instant = next(self._ticks)
            except StopIteration:
                raise asyncio.CancelledError from None
            self.sleeps.append(seconds)
            self.advance(instant - self.monotonic())
            self.tick += 1
            await asyncio.sleep(0)

    timers = TickTimers()
    clock = Clock(ClockConfig("UTC", "UTC", False), timers=timers, now=timers.now)
    sent_on_ticks: list[int] = []

    class RecordingDispatch(heartbeat_daemon.HeartbeatDispatch):
        def send_checkin(
            self, agent_id: int, idle_minutes: float, reason: str | None = None
        ) -> None:
            if agent_id == aid:
                sent_on_ticks.append(timers.tick)

    dispatch = RecordingDispatch(
        pool,
        database,
        event_bus,
        config=heartbeat_daemon.DispatchConfig(
            idle_threshold, settings.daemon.heartbeat_interval_seconds
        ),
        clock=clock,
    )
    with pytest.raises(asyncio.CancelledError):
        await heartbeat_daemon.dispatch_loop(
            dispatch, LoopProgress("dispatch", 60.0, timers=clock), timers=clock
        )
    assert sent_on_ticks == [0, 3]

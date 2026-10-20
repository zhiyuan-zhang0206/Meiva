from __future__ import annotations

import asyncio

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.events.contract import payload_keys
from base.events.live.bus import EventBus
from base.telemetry.tests.capture.event_capture import EmittedEvents
from base.telemetry.tests.capture.event_capture import emitted_events as emitted_events
from services.wake.heartbeat.liveness import report_stopped_too_long, run_liveness_pass
from services.wake.heartbeat.tests.machine_probe_setup import (
    FakeProbe,
    pool,
    register_machine,
)


# meiva:note
# Gives `name` an announced stop `minutes_ago`, optionally with a window ahead.
def _stop(
    db: psycopg.Connection,
    name: str,
    *,
    minutes_ago: int,
    window_minutes: int | None = None,
    paused: bool = False,
    staging: bool = False,
) -> None:
    register_machine(db, name)
    with db.cursor() as cur:
        cur.execute(
            "UPDATE machines SET stopped_at = now() - make_interval(mins => %s), "
            "  maintenance_until = CASE WHEN %s::int IS NULL THEN NULL "
            "    ELSE now() - make_interval(mins => %s) + make_interval(mins => %s::int) END, "
            "  maintenance_reason = CASE WHEN %s::int IS NULL THEN NULL ELSE 'planned' END, "
            "  paused_at = CASE WHEN %s THEN now() ELSE NULL END, "
            "  is_staging = %s "
            "WHERE name = %s",
            (
                minutes_ago,
                window_minutes,
                minutes_ago,
                window_minutes,
                window_minutes,
                paused,
                staging,
                name,
            ),
        )
    db.commit()


def _report(database_gate: ProcessDbGate) -> list[str]:
    return report_stopped_too_long(Database.from_settings(gate=database_gate))


@pytest.mark.usefixtures(pool.__name__)
class TestStoppedTooLongSignal:
    def test_a_host_stopped_past_the_threshold_is_reported_once_with_its_age(
        self,
        db_conn: psycopg.Connection,
        pool: ConnectionPool,
        emitted_events: EmittedEvents,
        *,
        database_gate: ProcessDbGate,
    ) -> None:
        _stop(db_conn, "forgotten", minutes_ago=21)

        assert _report(database_gate) == ["forgotten"]
        (event,) = emitted_events
        assert (event.category, event.event_name, event.level) == (
            "telemetry",
            "machine_stopped_too_long",
            "warning",
        )
        assert event.event_name == "machine_stopped_too_long"
        assert event.attributes["machine"] == "forgotten"
        assert 20 * 60 < event.attributes["stopped_for_s"] <= 21 * 60 + 5
        assert set(event.attributes) == set(payload_keys("machine_stopped_too_long"))

    def test_a_recent_stop_is_still_inside_a_normal_restart(
        self,
        db_conn: psycopg.Connection,
        pool: ConnectionPool,
        emitted_events: EmittedEvents,
        *,
        database_gate: ProcessDbGate,
    ) -> None:
        _stop(db_conn, "restarting", minutes_ago=5)

        assert _report(database_gate) == []
        assert len(emitted_events) == 0

    def test_a_declared_window_still_ahead_suppresses_the_report(
        self,
        db_conn: psycopg.Connection,
        pool: ConnectionPool,
        emitted_events: EmittedEvents,
        *,
        database_gate: ProcessDbGate,
    ) -> None:
        _stop(db_conn, "in-window", minutes_ago=25, window_minutes=45)

        assert _report(database_gate) == []
        assert len(emitted_events) == 0

    def test_an_expired_window_is_reported(
        self,
        db_conn: psycopg.Connection,
        pool: ConnectionPool,
        emitted_events: EmittedEvents,
        *,
        database_gate: ProcessDbGate,
    ) -> None:
        _stop(db_conn, "window-expired", minutes_ago=40, window_minutes=30)

        assert _report(database_gate) == ["window-expired"]
        assert [event.attributes["machine"] for event in emitted_events] == ["window-expired"]

    def test_a_host_that_never_announced_a_stop_is_not_this_signal(
        self,
        db_conn: psycopg.Connection,
        pool: ConnectionPool,
        emitted_events: EmittedEvents,
        *,
        database_gate: ProcessDbGate,
    ) -> None:
        register_machine(db_conn, "never-stopped")

        assert _report(database_gate) == []
        assert len(emitted_events) == 0

    @pytest.mark.parametrize("latch", ["paused", "staging"])
    def test_a_deliberately_away_host_is_silent(
        self,
        db_conn: psycopg.Connection,
        pool: ConnectionPool,
        emitted_events: EmittedEvents,
        latch: str,
        *,
        database_gate: ProcessDbGate,
    ) -> None:
        _stop(
            db_conn, "away", minutes_ago=120, paused=latch == "paused", staging=latch == "staging"
        )

        assert _report(database_gate) == []
        assert len(emitted_events) == 0

    # meiva:note
    # The signal is wired into the pass, not only available as a function.
    def test_the_liveness_pass_makes_the_report(
        self,
        db_conn: psycopg.Connection,
        pool: ConnectionPool,
        emitted_events: EmittedEvents,
        *,
        database_gate: ProcessDbGate,
    ) -> None:
        _stop(db_conn, "forgotten", minutes_ago=30)
        asyncio.run(
            run_liveness_pass(
                Database.from_settings(gate=database_gate),
                pool,
                EventBus.from_settings(),
                probe=FakeProbe({"forgotten": False}),
            )
        )

        names = [event.event_name for event in emitted_events]
        assert "machine_stopped_too_long" in names

# meiva:note
# The reaper's database-only sweep against the real Postgres fixture: expired
# browser sessions are reclaimed in bounded batches, and the fire-log prune
# deletes every claim past the window.

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from base.clock.tests.fakes import system_clock
from base.daemon.loop_health import LoopProgress
from base.db.code_version_gate import ProcessDbGate
from base.telemetry.tests.capture.event_capture import emitted_events as emitted_events
from services.upkeep.ttl_reaper import cadence, sweep
from services.upkeep.ttl_reaper.sweep import (
    reap_expired_web_sessions_blocking,
)
from services.upkeep.ttl_reaper.tests.slices import reaper_config


@pytest.fixture()
def reaper_pool(db_url: str, *, database_gate: ProcessDbGate) -> Iterator[ConnectionPool]:
    """The reaper's own small pool (the functions take a ConnectionPool)."""
    import base.db

    pool = base.db.pool(max_size=2, gate=database_gate)
    yield pool
    pool.close()


def test_reap_expired_web_sessions_removes_only_expired_rows(
    db_conn: psycopg.Connection, reaper_pool: ConnectionPool
) -> None:
    """The periodic pass, not login traffic, reclaims expired browser sessions."""
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO web_sessions (id, expires_at) VALUES (%s, now() - interval '1 second')",
            ("expired-session",),
        )
        cur.execute(
            "INSERT INTO web_sessions (id, expires_at) VALUES (%s, now() + interval '1 hour')",
            ("live-session",),
        )
    db_conn.commit()

    assert reap_expired_web_sessions_blocking(reaper_pool) == 1

    with db_conn.cursor() as cur:
        cur.execute("SELECT id FROM web_sessions ORDER BY id")
        assert [row[0] for row in cur.fetchall()] == ["live-session"]


def test_reap_expired_web_sessions_is_limited_to_one_pass_batch(
    db_conn: psycopg.Connection,
    reaper_pool: ConnectionPool,
) -> None:
    """A large expired-session backlog is reclaimed across bounded transactions."""
    with db_conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO web_sessions (id, expires_at) VALUES (%s, now() + %s::interval)",
            [
                ("older-expired-session", "-2 seconds"),
                ("newer-expired-session", "-1 second"),
                ("live-session", "1 hour"),
            ],
        )
    db_conn.commit()

    assert reap_expired_web_sessions_blocking(reaper_pool, batch_size=1) == 1

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM web_sessions WHERE id = ANY(%s) ORDER BY id",
            (["older-expired-session", "newer-expired-session", "live-session"],),
        )
        assert [row[0] for row in cur.fetchall()] == ["live-session", "newer-expired-session"]


def _new_schedule(conn: psycopg.Connection, name: str) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO schedules (name, script, command, machine) VALUES (%s, 'x', 'x', 'gateway') RETURNING id",
            (name,),
        )
        row = cur.fetchone()
        assert row is not None
        return int(row[0])


def _insert_fire_log(conn: psycopg.Connection, schedule_id: int, age_hours: list[int]) -> None:
    with conn.cursor() as cur:
        for hours in age_hours:
            cur.execute(
                "INSERT INTO schedule_fire_log (schedule_id, slot_fire_at) "
                "VALUES (%s, now() - %s * interval '1 hour')",
                (schedule_id, hours),
            )


def test_prune_schedule_fire_log_deletes_every_row_past_the_window(
    db_conn: psycopg.Connection, reaper_pool: ConnectionPool
) -> None:
    """The retention prune deletes every claim past the window, including the newest
    claim of a schedule whose whole log is older than it; rows inside the window stay."""
    s1 = _new_schedule(db_conn, "fire-log-s1")
    _insert_fire_log(db_conn, s1, [40 * 24 + 2, 40 * 24 + 1, 40 * 24, 1])
    s2 = _new_schedule(db_conn, "fire-log-s2")
    _insert_fire_log(db_conn, s2, [50 * 24, 45 * 24])
    db_conn.commit()

    pruned = sweep.prune_schedule_fire_log_blocking(
        reaper_pool, retention_days=30, now=datetime.now(UTC)
    )

    assert pruned == 5
    with db_conn.cursor() as cur:
        cur.execute("SELECT schedule_id, slot_fire_at FROM schedule_fire_log")
        rows = cur.fetchall()
    # Only s1's 1-hour-old claim is inside the window; s2 keeps nothing.
    assert len(rows) == 1
    assert rows[0][0] == s1 and rows[0][1] > datetime.now(UTC) - timedelta(hours=2)


def test_prune_schedule_fire_log_is_bounded_per_pass(
    db_conn: psycopg.Connection,
    reaper_pool: ConnectionPool,
) -> None:
    """One pass deletes at most FIRE_LOG_PASS_BATCH rows; the backlog drains
    over successive passes."""
    s1 = _new_schedule(db_conn, "fire-log-bounded")
    _insert_fire_log(db_conn, s1, [50 * 24, 49 * 24, 48 * 24, 47 * 24, 46 * 24])
    db_conn.commit()
    now = datetime.now(UTC)

    assert (
        sweep.prune_schedule_fire_log_blocking(
            reaper_pool, retention_days=30, now=now, batch_size=2
        )
        == 2
    )
    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM schedule_fire_log WHERE schedule_id = %s", (s1,))
        remaining = cur.fetchone()
        assert remaining is not None and remaining[0] == 3

    assert (
        sweep.prune_schedule_fire_log_blocking(
            reaper_pool, retention_days=30, now=now, batch_size=2
        )
        == 2
    )
    assert (
        sweep.prune_schedule_fire_log_blocking(
            reaper_pool, retention_days=30, now=now, batch_size=2
        )
        == 1
    )
    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM schedule_fire_log WHERE schedule_id = %s", (s1,))
        remaining = cur.fetchone()
        assert remaining is not None and remaining[0] == 0


def _backdate(db: psycopg.Connection, kind: str, seconds: float) -> None:
    db.execute(
        "UPDATE maintenance_state SET last_run_at = clock_timestamp() - make_interval(secs => %s) "
        "WHERE kind = %s",
        (seconds, kind),
    )
    db.commit()


def _fire_log_rows(conn: psycopg.Connection, schedule_id: int) -> int:
    row = conn.execute(
        "SELECT count(*) FROM schedule_fire_log WHERE schedule_id = %s", (schedule_id,)
    ).fetchone()
    assert row is not None
    return int(row[0])


async def test_the_fire_log_prune_runs_once_per_cadence_not_once_per_round(
    db_conn: psycopg.Connection,
    reaper_pool: ConnectionPool,
) -> None:
    schedule = _new_schedule(db_conn, "fire-log-cadence")
    _insert_fire_log(db_conn, schedule, [40 * 24])
    db_conn.commit()

    async def round_() -> None:
        await sweep.sweep_round(
            reaper_pool,
            LoopProgress("test", 60.0, clock=system_clock()),
            reaper_config(fire_log_cleanup_interval_s=86400.0),
        )

    await round_()
    assert _fire_log_rows(db_conn, schedule) == 0

    _insert_fire_log(db_conn, schedule, [41 * 24])
    db_conn.commit()
    await round_()
    assert _fire_log_rows(db_conn, schedule) == 1, "the cadence was not due again"

    _backdate(db_conn, cadence.FIRE_LOG_PRUNE, 90000.0)
    await round_()
    assert _fire_log_rows(db_conn, schedule) == 0


async def test_the_prune_keeps_what_the_configured_retention_keeps(
    db_conn: psycopg.Connection,
    reaper_pool: ConnectionPool,
) -> None:
    schedule = _new_schedule(db_conn, "fire-log-retention")
    _insert_fire_log(db_conn, schedule, [3 * 24, 10 * 24])
    db_conn.commit()

    await sweep.sweep_round(
        reaper_pool,
        LoopProgress("test", 60.0, clock=system_clock()),
        reaper_config(fire_log_retention_days=7),
    )

    assert _fire_log_rows(db_conn, schedule) == 1

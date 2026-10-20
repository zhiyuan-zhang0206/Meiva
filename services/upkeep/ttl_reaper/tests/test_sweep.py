# meiva:note
# The reaper's database-only sweep against the real Postgres fixture: page rows
# are terminalized (expired_at, never closed_at), unexpired/closed rows are left
# alone, owners are notified only when running/idling, expired browser sessions
# and notices are reclaimed in bounded batches, the fire-log prune deletes every
# claim past the window.

from __future__ import annotations

import itertools
import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from base.daemon.loop_health import LoopProgress
from base.db import Database, create_agent
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus, event_bus_config_from_settings
from base.telemetry.tests.capture.event_capture import emitted_events as emitted_events
from services.upkeep.ttl_reaper import sweep
from services.upkeep.ttl_reaper.sweep import (
    reap_expired_pages_blocking,
    reap_expired_web_sessions_blocking,
)


@pytest.fixture()
def reaper_pool(db_url: str, *, database_gate: ProcessDbGate) -> Iterator[ConnectionPool]:
    """The reaper's own small pool (the functions take a ConnectionPool)."""
    import base.db

    pool = base.db.pool(max_size=2, gate=database_gate)
    yield pool
    pool.close()


# One distinct port per row: an open (host, port) pair is unique since
# agent_pages_unique_live_port (20260920) — the reaper never cares about the
# port value, and a shared fixed port now collides between pages.
_PAGE_PORTS = itertools.count(8001)


def _open_page(
    conn: psycopg.Connection,
    agent_id: int,
    name: str,
    *,
    expires_at: datetime | None,
) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_pages (agent_id, name, port, host, expires_at) "
            "VALUES (%s, %s, %s, '127.0.0.1', %s) RETURNING id",
            (agent_id, name, next(_PAGE_PORTS), expires_at),
        )
        row = cur.fetchone()
    conn.commit()
    assert row is not None
    return row[0]


def _page_state(
    conn: psycopg.Connection, agent_id: int, name: str
) -> tuple[datetime | None, datetime | None]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT closed_at, expired_at FROM agent_pages WHERE agent_id = %s AND name = %s",
            (agent_id, name),
        )
        row = cur.fetchone()
    assert row is not None
    return row[0], row[1]


def _running_agent(conn: psycopg.Connection) -> int:
    aid = create_agent(conn)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agents_meta (id, spawner, status) VALUES (%s, 'user', 'running')",
            (aid,),
        )
    conn.commit()
    return aid


def _system_inbounds(conn: psycopg.Connection, agent_id: int) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT content FROM inbound_messages WHERE agent_id = %s AND source = 'system'",
            (agent_id,),
        )
        return [r[0] for r in cur.fetchall()]


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
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A large expired-session backlog is reclaimed across bounded transactions."""
    monkeypatch.setattr(sweep, "PASS_BATCH", 1)
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

    assert reap_expired_web_sessions_blocking(reaper_pool) == 1

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM web_sessions WHERE id = ANY(%s) ORDER BY id",
            (["older-expired-session", "newer-expired-session", "live-session"],),
        )
        assert [row[0] for row in cur.fetchall()] == ["live-session", "newer-expired-session"]


def test_reap_expired_pages_terminalizes_only_past_deadlines(
    db_conn: psycopg.Connection,
    reaper_pool: ConnectionPool,
    database: Database,
    event_bus: EventBus,
) -> None:
    aid = _running_agent(db_conn)
    _open_page(db_conn, aid, "expired", expires_at=datetime.now(UTC) - timedelta(seconds=5))
    _open_page(db_conn, aid, "future", expires_at=datetime.now(UTC) + timedelta(hours=1))
    closed_id = _open_page(
        db_conn, aid, "closed", expires_at=datetime.now(UTC) - timedelta(seconds=5)
    )
    with db_conn.cursor() as cur:
        cur.execute("UPDATE agent_pages SET closed_at = now() WHERE id = %s", (closed_id,))
    db_conn.commit()

    reaped = reap_expired_pages_blocking(reaper_pool, database, event_bus)

    assert [(aid, "expired")] == [(a, n) for a, n, _i in reaped]
    expired_closed, expired_marked = _page_state(db_conn, aid, "expired")
    assert expired_closed is None and expired_marked is not None
    future_closed, future_marked = _page_state(db_conn, aid, "future")
    assert future_closed is None and future_marked is None
    closed_closed, closed_marked = _page_state(db_conn, aid, "closed")
    assert closed_closed is not None and closed_marked is None


def test_reap_expired_pages_skips_already_terminal(
    db_conn: psycopg.Connection, reaper_pool: ConnectionPool, database: Database
) -> None:
    aid = _running_agent(db_conn)
    _open_page(db_conn, aid, "expired", expires_at=datetime.now(UTC) - timedelta(seconds=5))
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE agent_pages SET expired_at = now() WHERE agent_id = %s AND name = 'expired'",
            (aid,),
        )
    db_conn.commit()

    assert reap_expired_pages_blocking(reaper_pool, database, EventBus.from_settings()) == []


def test_reap_expired_pages_notifies_only_live_agents(
    db_conn: psycopg.Connection,
    reaper_pool: ConnectionPool,
    database: Database,
    event_bus: EventBus,
) -> None:
    live = _running_agent(db_conn)
    dead = create_agent(db_conn)
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agents_meta (id, spawner, status) VALUES (%s, 'user', 'terminated')",
            (dead,),
        )
    db_conn.commit()
    _open_page(db_conn, live, "live-page", expires_at=datetime.now(UTC) - timedelta(seconds=5))
    _open_page(db_conn, dead, "dead-page", expires_at=datetime.now(UTC) - timedelta(seconds=5))

    reap_expired_pages_blocking(reaper_pool, database, event_bus)

    live_msgs = _system_inbounds(db_conn, live)
    assert len(live_msgs) == 1
    assert "reclaimed after its TTL" in live_msgs[0]
    assert _system_inbounds(db_conn, dead) == []


def _new_schedule(conn: psycopg.Connection, name: str) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO schedules (name, script, command) VALUES (%s, 'x', 'x') RETURNING id",
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

    pruned = sweep.prune_schedule_fire_log_blocking(reaper_pool)

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
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One pass deletes at most FIRE_LOG_PASS_BATCH rows; the backlog drains
    over successive passes."""
    monkeypatch.setattr(sweep, "FIRE_LOG_PASS_BATCH", 2)
    s1 = _new_schedule(db_conn, "fire-log-bounded")
    _insert_fire_log(db_conn, s1, [50 * 24, 49 * 24, 48 * 24, 47 * 24, 46 * 24])
    db_conn.commit()

    assert sweep.prune_schedule_fire_log_blocking(reaper_pool) == 2
    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM schedule_fire_log WHERE schedule_id = %s", (s1,))
        remaining = cur.fetchone()
        assert remaining is not None and remaining[0] == 3

    assert sweep.prune_schedule_fire_log_blocking(reaper_pool) == 2
    assert sweep.prune_schedule_fire_log_blocking(reaper_pool) == 1
    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM schedule_fire_log WHERE schedule_id = %s", (s1,))
        remaining = cur.fetchone()
        assert remaining is not None and remaining[0] == 0


class _RecordingBus(EventBus):
    def __init__(self) -> None:
        super().__init__(event_bus_config_from_settings())
        self.payloads: list[dict[str, object]] = []

    async def publish_best_effort(
        self, payload: str, *, channel: str | None = None, context: str = ""
    ) -> int | None:
        self.payloads.append(json.loads(payload))
        return 1


async def test_expired_notice_publishes_observation_facts(
    db_conn: psycopg.Connection,
    reaper_pool: ConnectionPool,
    database: Database,
    event_bus: EventBus,
) -> None:
    aid = _running_agent(db_conn)
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_notices "
            "(agent_id, local_id, title, priority, require_response, expire_at) "
            "VALUES (%s, 0, 'expired?', 'P2', true, now() - interval '1 minute') "
            "RETURNING id",
            (aid,),
        )
        row = cur.fetchone()
        assert row is not None
        nid = int(row[0])
    db_conn.commit()

    bus = _RecordingBus()
    await sweep.sweep_round(reaper_pool, database, bus, LoopProgress("test", 60.0))

    resolved = [event for event in bus.payloads if event.get("role") == "notice_resolved"]
    assert len(resolved) == 1
    facts = resolved[0]
    assert facts["notice_id"] == nid
    assert facts["resolution"] == "expired"
    assert facts["require_response"] is True
    assert facts["had_reply"] is False
    assert isinstance(facts["age_seconds"], int) and facts["age_seconds"] >= 0

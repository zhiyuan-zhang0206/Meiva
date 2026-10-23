from __future__ import annotations

import json
import uuid
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import psycopg
from psycopg_pool import ConnectionPool

from base.telemetry.tests.capture.event_capture import EmittedEvents
from base.telemetry.tests.capture.event_capture import emitted_events as emitted_events
from gateway.lgtm import telemetry_staleness
from gateway.lgtm.telemetry_staleness import TelemetryStaleness


class _Pool:
    def __init__(self, conn: psycopg.Connection) -> None:
        self._conn = conn

    @contextmanager
    def connection(self) -> Generator[psycopg.Connection]:
        yield self._conn


def _heartbeat(conn: psycopg.Connection, at: datetime, *, event: str = "gateway_latency") -> None:
    conn.execute(
        "INSERT INTO telemetry_events (event_uid, ts, machine, cluster, process, category, "
        "event_name, level, source, attributes) VALUES (%s, %s, 'm', 'c', 'p', 'telemetry', %s, "
        "'info', 'test', %s::jsonb)",
        (uuid.uuid4().int % (1 << 62), at, event, json.dumps({})),
    )


def test_heartbeat_age_reads_the_newest_heartbeat_row(db_conn: psycopg.Connection) -> None:
    db_conn.autocommit = True
    now = datetime(2001, 3, 4, 12, 0, tzinfo=UTC) + timedelta(seconds=uuid.uuid4().int % 100000)
    pool = _Pool(db_conn)

    assert telemetry_staleness.heartbeat_age(pool, now=now) is None

    _heartbeat(db_conn, now - timedelta(seconds=90))
    _heartbeat(db_conn, now - timedelta(seconds=30))
    _heartbeat(db_conn, now - timedelta(seconds=5), event="llm_usage")
    assert telemetry_staleness.heartbeat_age(pool, now=now) == 30.0

    later = now + timedelta(seconds=2 * telemetry_staleness.STALENESS_THRESHOLD_S + 31)
    assert telemetry_staleness.heartbeat_age(pool, now=later) is None


def test_check_reports_stale_rate_limits_and_recovers(
    db_conn: psycopg.Connection, emitted_events: EmittedEvents
) -> None:
    db_conn.autocommit = True
    started = datetime(2001, 3, 4, 12, 0, tzinfo=UTC) + timedelta(seconds=uuid.uuid4().int % 100000)
    pool = _Pool(db_conn)
    guard = TelemetryStaleness(check_interval_s=0)

    _heartbeat(db_conn, started - timedelta(seconds=30))
    assert guard.check_and_report(pool, now=started) is False
    assert emitted_events.names() == []

    # meiva:note
    # The heartbeat falls out of the window the guard reads: missing.
    missing = started + timedelta(seconds=2 * telemetry_staleness.STALENESS_THRESHOLD_S + 31)
    assert guard.check_and_report(pool, now=missing) is True
    assert emitted_events.names() == ["telemetry_read_stale"]
    assert emitted_events.attributes("telemetry_read_stale") == [
        {
            "source": "postgres",
            "signal": "gateway_latency",
            "threshold_s": 300,
            "age_s": None,
            "action": "entered",
            "reason": "heartbeat missing",
        }
    ]

    assert guard.check_and_report(pool, now=missing + timedelta(seconds=299))
    assert len(emitted_events) == 1

    assert guard.check_and_report(pool, now=missing + timedelta(seconds=300))
    assert emitted_events.names() == ["telemetry_read_stale"] * 2
    assert emitted_events.attributes("telemetry_read_stale")[-1]["action"] == "ongoing"

    recovered_at = missing + timedelta(seconds=360)
    _heartbeat(db_conn, recovered_at - timedelta(seconds=30))
    assert guard.check_and_report(pool, now=recovered_at) is False
    assert emitted_events.names()[-1] == "telemetry_read_recovered"
    assert emitted_events.attributes("telemetry_read_recovered") == [
        {"source": "postgres", "signal": "gateway_latency", "stale_duration_s": 360.0}
    ]


def test_check_throttles_heartbeat_queries(db_conn: psycopg.Connection) -> None:
    db_conn.autocommit = True
    now = datetime(2001, 3, 4, 12, 0, tzinfo=UTC) + timedelta(seconds=uuid.uuid4().int % 100000)
    pool = _Pool(db_conn)
    guard = TelemetryStaleness(check_interval_s=60)

    # meiva:note
    # No heartbeat row yet: the first check reads stale and the verdict is held for the interval,
    # even after a heartbeat appears, until the interval has passed.
    assert guard.check_and_report(pool, now=now) is True
    _heartbeat(db_conn, now - timedelta(seconds=5))
    assert guard.check_and_report(pool, now=now + timedelta(seconds=59)) is True
    assert guard.check_and_report(pool, now=now + timedelta(seconds=60)) is False


def test_check_fail_open_when_the_heartbeat_query_fails(emitted_events: EmittedEvents) -> None:
    guard = TelemetryStaleness(check_interval_s=0)

    # meiva:note
    # A pool that was never opened fails every borrow, as a lost database does.
    unopened = ConnectionPool("postgresql://127.0.0.1:1/none", open=False)

    assert guard.check_and_report(unopened, now=datetime(2026, 8, 23, 12, 0, tzinfo=UTC)) is False
    assert emitted_events.names() == []

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from psycopg_pool import ConnectionPool

import base.db
from base.config import ConfigBoot
from base.db.code_version_gate import ProcessDbGate
from base.telemetry.tests.capture.event_capture import emitted_events as emitted_events
from services.upkeep.events_maintenance import resolution
from services.upkeep.events_maintenance.config import EventsMaintenanceConfig
from services.upkeep.events_maintenance.tests.db_faults import table_missing, write_fails
from services.upkeep.events_maintenance.tests.slices import events_maintenance_config
from services.upkeep.events_maintenance.tests.test_events_maintenance_resolution import (
    event_class,
    record_maintenance_event,
)


@pytest.fixture
def pool(db_url: str, *, database_gate: ProcessDbGate) -> Iterator[ConnectionPool]:
    p = base.db.pool(max_size=2, gate=database_gate)
    try:
        yield p
    finally:
        p.close()


def _history(connection: psycopg.Connection[Any]) -> datetime:
    connection.execute("TRUNCATE event_dismissals")
    connection.commit()
    for slot in range(4):
        record_maintenance_event(connection, event_name="steady", minutes_ago=slot * 360 + 1)
    return datetime.now(UTC)


def _dismissals(connection: psycopg.Connection[Any]) -> int:
    connection.rollback()
    row = connection.execute("SELECT count(*) FROM event_dismissals").fetchone()
    assert row is not None
    return int(row[0])


def _enabled(process_config: ConfigBoot) -> EventsMaintenanceConfig:
    return events_maintenance_config(
        process_config, events_auto_dismiss_enabled=True, events_auto_dismiss_days=1
    )


@pytest.mark.usefixtures("emitted_events")
def test_a_failed_read_leaves_the_day_to_be_retried(
    process_config: ConfigBoot, db_conn: psycopg.Connection[Any], pool: ConnectionPool
) -> None:
    now = _history(db_conn)
    cadence = resolution.AutoDismissCadence()

    with table_missing(db_conn, "telemetry_events"):
        assert (
            resolution.run_resolution_slice(
                pool, _enabled(process_config), cadence=cadence, now=now
            )
            is None
        )
    assert cadence.due(now)

    result = resolution.run_resolution_slice(
        pool, _enabled(process_config), cadence=cadence, now=now
    )
    assert result is not None and result.auto_dismissed == 1
    assert not cadence.due(now)
    assert _dismissals(db_conn) == 1


@pytest.mark.usefixtures("emitted_events")
def test_a_failed_write_consumes_the_day(
    process_config: ConfigBoot, db_conn: psycopg.Connection[Any], pool: ConnectionPool
) -> None:
    now = _history(db_conn)
    cadence = resolution.AutoDismissCadence()

    with (
        write_fails(db_conn, "event_dismissals", on="INSERT"),
        pytest.raises(psycopg.errors.RaiseException),
    ):
        resolution.run_resolution_slice(pool, _enabled(process_config), cadence=cadence, now=now)
    assert not cadence.due(now)

    result = resolution.run_resolution_slice(
        pool, _enabled(process_config), cadence=cadence, now=now
    )
    assert result is not None and result.auto_dismissed == 0
    assert _dismissals(db_conn) == 0


@pytest.mark.usefixtures("emitted_events")
def test_each_cadence_scans_once_a_day_and_a_new_one_scans_again(
    process_config: ConfigBoot, db_conn: psycopg.Connection[Any], pool: ConnectionPool
) -> None:
    now = _history(db_conn)
    config = _enabled(process_config)
    cadence = resolution.AutoDismissCadence()

    resolution.run_resolution_slice(pool, config, cadence=cadence, now=now)
    assert _dismissals(db_conn) == 1

    db_conn.execute("TRUNCATE event_dismissals")
    db_conn.commit()
    resolution.run_resolution_slice(pool, config, cadence=cadence, now=now)
    assert _dismissals(db_conn) == 0, "the same loop must not scan the same day twice"

    resolution.run_resolution_slice(pool, config, cadence=resolution.AutoDismissCadence(), now=now)
    assert _dismissals(db_conn) == 1, "another loop owns a fresh daily scan"

    tomorrow = now + timedelta(days=1)
    assert cadence.due(tomorrow)
    resolution.stable_auto_classes(tomorrow, db_conn, {}, config, cadence)
    assert not cadence.due(tomorrow)


def test_disabled_scan_does_not_consume_cadence(
    process_config: ConfigBoot, db_conn: psycopg.Connection[Any]
) -> None:
    now = _history(db_conn)
    cadence = resolution.AutoDismissCadence()

    stable = resolution.stable_auto_classes(
        now, db_conn, {event_class(): 1}, events_maintenance_config(process_config), cadence
    )

    assert stable == set()
    assert cadence.due(now)

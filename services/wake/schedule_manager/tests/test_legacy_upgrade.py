from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from base.clock.tests.fakes import clock_at
from base.cluster import session_name
from base.config import settings
from base.native_process.process_control import SYSTEM_PROCESS_CONTROL
from services.wake.schedule_manager import requests
from services.wake.schedule_manager.declarative import DeclarativeScheduleEngine
from services.wake.schedule_manager.manager import REPO_ROOT
from services.wake.schedule_manager.run_custody import SYSTEM_PROCESS_IDENTITY_CONTROL
from services.wake.schedule_manager.tests.manager_support import NOW, schedule_manager
from services.wake.schedule_manager.tests.session_fakes import Backend


@pytest.fixture
def pool(db_url: str) -> Iterator[ConnectionPool[psycopg.Connection]]:
    del db_url
    active: ConnectionPool[psycopg.Connection] = ConnectionPool(
        settings.data_plane.db_url, min_size=1, max_size=2
    )
    with active:
        yield active


@pytest.mark.parametrize(
    ("trigger_kind", "cron_expr", "interval_seconds"),
    [("cron", "* * * * *", None), ("every", None, 60)],
)
@pytest.mark.parametrize("session_live", [True, False])
def test_upgrade_reconcile_before_sync_preserves_legacy_run_until_reap(
    db_conn: psycopg.Connection,
    pool: ConnectionPool[psycopg.Connection],
    unit_home: Path,
    trigger_kind: str,
    session_live: bool,
    cron_expr: str | None,
    interval_seconds: int | None,
) -> None:
    now = datetime(2026, 11, 17, tzinfo=UTC)
    row = db_conn.execute(
        "INSERT INTO schedules (name, script, command, machine, enabled, trigger_kind, status) "
        "VALUES ('legacy-upgrade', 'pass', 'python run.py', 'gateway', true, 'daemon', 'running') "
        "RETURNING id"
    ).fetchone()
    assert row is not None
    sid = row[0]
    legacy = db_conn.execute(
        "INSERT INTO schedule_runs (schedule_id, ran_at) VALUES (%s, %s) RETURNING id",
        (sid, now - timedelta(minutes=1)),
    ).fetchone()
    assert legacy is not None
    db_conn.execute(
        "UPDATE schedules SET trigger_kind = %s, cron_expr = %s, interval_seconds = %s, "
        "status = 'scheduled', next_launch_at = %s WHERE id = %s",
        (trigger_kind, cron_expr, interval_seconds, now, sid),
    )
    db_conn.execute(
        "INSERT INTO schedule_sync_requests (schedule_id, machine) VALUES (%s, 'gateway')", (sid,)
    )
    db_conn.commit()
    backend = Backend()
    name = session_name(f"schedule-{sid}")
    if session_live:
        backend.hold(name, "legacy daemon")
    manager = schedule_manager(pool, backend, home=unit_home)

    engine = DeclarativeScheduleEngine(
        pool,
        home=unit_home,
        repo_root=REPO_ROOT,
        machine_name="gateway",
        launch_env={},
        clock=clock_at(now),
        secret_env_resolver=None,
        process_control=SYSTEM_PROCESS_CONTROL,
        identity_control=SYSTEM_PROCESS_IDENTITY_CONTROL,
    )
    engine.reconcile()

    assert db_conn.execute(
        "SELECT ok, note FROM schedule_runs WHERE id = %s", (legacy[0],)
    ).fetchone() == (None, None)
    assert db_conn.execute("SELECT status FROM schedules WHERE id = %s", (sid,)).fetchone() == (
        "scheduled",
    )
    manager.reconcile(now=NOW)
    assert requests.consume_requests(pool, manager, now=NOW) == 1
    assert not backend.has_session(name)
    assert db_conn.execute(
        "SELECT ok, note FROM schedule_runs WHERE id = %s", (legacy[0],)
    ).fetchone() == (False, "interrupted")
    assert db_conn.execute("SELECT status FROM schedules WHERE id = %s", (sid,)).fetchone() == (
        "scheduled",
    )
    claimed = engine._claim_due(now)
    assert claimed is not None
    declarative = db_conn.execute(
        "SELECT id FROM schedule_runs WHERE schedule_id = %s AND scheduled_for IS NOT NULL", (sid,)
    ).fetchone()
    assert declarative is not None
    assert manager.sync_one(sid, now=NOW)
    assert db_conn.execute(
        "SELECT ok FROM schedule_runs WHERE id = %s", (declarative[0],)
    ).fetchone() == (None,)

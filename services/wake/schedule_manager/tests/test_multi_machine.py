# meiva:note
# Split-unit election prevents duplicate declarative launches on one machine.

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
from psycopg_pool import ConnectionPool

from base.clock.tests.fakes import clock_at
from base.native_process.process_control import SYSTEM_PROCESS_CONTROL
from services.wake.schedule_manager.manager import REPO_ROOT, ScheduleManager
from services.wake.schedule_manager.revisions import is_active_unit


def test_colocated_split_unit_tie_breaker(
    db_conn: psycopg.Connection, db_url: str, tmp_path: Path
) -> None:
    home_a, home_b = tmp_path / "a", tmp_path / "b"
    with ConnectionPool[Any](db_url, min_size=1, max_size=2) as pool:
        assert is_active_unit(pool, "shared-host", home_a)
        db_conn.execute(
            "INSERT INTO machine_units (machine_name, home, serve_gateway) VALUES "
            "('shared-host', %s, false), ('shared-host', %s, true)",
            (str(home_a), str(home_b)),
        )
        db_conn.commit()
        assert not is_active_unit(pool, "shared-host", home_a)
        assert is_active_unit(pool, "shared-host", home_b)
        db_conn.execute(
            "UPDATE machine_units SET stopped_at = now() WHERE machine_name = 'shared-host' AND home = %s",
            (str(home_b),),
        )
        db_conn.commit()
        assert is_active_unit(pool, "shared-host", home_a)
        assert not is_active_unit(pool, "shared-host", home_b)


def test_unelected_manager_leaves_due_fire_for_elected_home(
    db_conn: psycopg.Connection, db_url: str, tmp_path: Path
) -> None:
    home_a, home_b = tmp_path / "a", tmp_path / "b"
    now = datetime.now(UTC)
    db_conn.execute(
        "INSERT INTO machine_units (machine_name, home, serve_gateway) VALUES "
        "('shared-host', %s, true), ('shared-host', %s, false)",
        (str(home_a), str(home_b)),
    )
    row = db_conn.execute(
        "INSERT INTO schedules (name, script, command, enabled, status, machine, trigger_kind, "
        "run_at, next_launch_at, timezone, timeout_seconds) VALUES "
        "('elected-fire', 'print(1)', 'python run.py', true, 'scheduled', 'shared-host', 'at', "
        "%s, %s, 'UTC', 10) RETURNING id",
        (now, now),
    ).fetchone()
    assert row is not None
    db_conn.commit()
    with ConnectionPool[Any](db_url, min_size=1, max_size=2) as pool:
        manager = ScheduleManager(
            pool,
            repo_root=REPO_ROOT,
            home=home_b,
            launch_env={},
            machine_name="shared-host",
            process_control=SYSTEM_PROCESS_CONTROL,
            clock=clock_at(now),
        )
        manager.reconcile()
        assert db_conn.execute(
            "SELECT count(*) FROM schedule_runs WHERE schedule_id = %s", (row[0],)
        ).fetchone() == (0,)

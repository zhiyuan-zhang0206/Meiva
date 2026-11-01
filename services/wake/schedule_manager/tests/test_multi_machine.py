from __future__ import annotations

from pathlib import Path
from typing import Any

import psycopg
from psycopg_pool import ConnectionPool

from base.deploy.lifecycle import start_serving
from base.deploy.lifecycle.tests.serving_root import observe
from base.deploy.lifecycle.tests.serving_root import serving_root as serving_root
from base.paths.tests.homes import unit_home as unit_home
from base.sessions.protocol import SessionBackend
from services.wake.schedule_manager import requests
from services.wake.schedule_manager.manager import REPO_ROOT, ScheduleManager
from services.wake.schedule_manager.tests.manager_support import NOW, POLICY


class _MultiMachineBackend(SessionBackend):
    def __init__(self, host_gen: str) -> None:
        self.live: list[str] = []
        self.killed: list[str] = []
        self.launched: list[int] = []
        self._host_gen = host_gen

    def list_sessions(self, prefix: str = "") -> list[str]:
        return [s for s in self.live if s.startswith(prefix)]

    def has_session(self, name: str) -> bool:
        return name in self.live

    def session_generation(self, name: str) -> str | None:
        return self._host_gen if name in self.live else None

    def host_generation(self) -> str | None:
        return self._host_gen

    def new_session(
        self,
        name: str,
        cmd: str,
        cwd: Path,
        *,
        env: dict[str, str],
        login_shell: bool = True,
        exec_cmd: bool = True,
    ) -> bool:
        sid = int(str(env["MEIVA_SCHEDULE_ID"]))
        self.launched.append(sid)
        self.live.append(name)
        return True

    def kill_session(
        self,
        name: str,
        *,
        graceful: bool = False,
        timeout: float = 15.0,
        expected: bool = False,
    ) -> tuple[bool, str]:
        if name in self.live:
            self.live.remove(name)
            self.killed.append(name)
            return True, "killed"
        return True, "missing"


def _insert(conn: psycopg.Connection, name: str, *, machine: str, enabled: bool = True) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO schedules (name, script, command, enabled, status, machine) "
            "VALUES (%s, 'print(1)', 'python schedule.py', %s, 'stopped', %s) RETURNING id",
            (name, enabled, machine),
        )
        row = cur.fetchone()
    conn.commit()
    assert row is not None
    return row[0]


def test_independent_managers_partition_schedules_and_requests_by_machine(
    db_conn: psycopg.Connection, db_url: str, tmp_path: Path, serving_root: start_serving.RootBirth
) -> None:
    pool: ConnectionPool[Any] = ConnectionPool(db_url, min_size=1, max_size=4, open=True)
    try:
        home_a = tmp_path / "home-a"
        home_b = tmp_path / "home-b"
        home_a.mkdir()
        home_b.mkdir()

        gen_a = start_serving.begin_start(home_a)
        assert start_serving.mark_serving(
            home_a, gen_a, runtime=serving_root.runtime, observe=observe
        )
        gen_b = start_serving.begin_start(home_b)
        assert start_serving.mark_serving(
            home_b, gen_b, runtime=serving_root.runtime, observe=observe
        )

        backend_a = _MultiMachineBackend(gen_a)
        backend_b = _MultiMachineBackend(gen_b)

        mgr_a = ScheduleManager(
            pool,
            backend=backend_a,
            repo_root=REPO_ROOT,
            home=home_a,
            policy=POLICY,
            launch_env={},
            machine_name="machine-a",
            observe=observe,
        )
        mgr_b = ScheduleManager(
            pool,
            backend=backend_b,
            repo_root=REPO_ROOT,
            home=home_b,
            policy=POLICY,
            launch_env={},
            machine_name="machine-b",
            observe=observe,
        )

        sid_a = _insert(db_conn, "task-a", machine="machine-a", enabled=True)
        sid_b = _insert(db_conn, "task-b", machine="machine-b", enabled=True)

        mgr_a.reconcile(now=NOW)
        assert backend_a.launched == [sid_a]
        assert backend_b.launched == []

        mgr_b.reconcile(now=NOW)
        assert backend_a.launched == [sid_a]
        assert backend_b.launched == [sid_b]

        with db_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO schedule_sync_requests (schedule_id, machine) VALUES (%s, %s), (%s, %s)",
                (sid_a, "machine-a", sid_b, "machine-b"),
            )
        db_conn.commit()

        consumed_a = requests.consume_requests(pool, mgr_a, now=NOW)
        assert consumed_a == 1
        with db_conn.cursor() as cur:
            cur.execute("SELECT schedule_id, machine FROM schedule_sync_requests")
            remaining = cur.fetchall()
        assert remaining == [(sid_b, "machine-b")]

        consumed_b = requests.consume_requests(pool, mgr_b, now=NOW)
        assert consumed_b == 1
        with db_conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM schedule_sync_requests")
            row = cur.fetchone()
            assert row is not None and row[0] == 0
    finally:
        pool.close()


def test_colocated_split_unit_tie_breaker(
    db_conn: psycopg.Connection, db_url: str, tmp_path: Path, serving_root: start_serving.RootBirth
) -> None:
    pool: ConnectionPool[Any] = ConnectionPool(db_url, min_size=1, max_size=4, open=True)
    try:
        home_1 = tmp_path / "home-1"
        home_2 = tmp_path / "home-2"
        home_1.mkdir()
        home_2.mkdir()

        gen_1 = start_serving.begin_start(home_1)
        assert start_serving.mark_serving(
            home_1, gen_1, runtime=serving_root.runtime, observe=observe
        )
        gen_2 = start_serving.begin_start(home_2)
        assert start_serving.mark_serving(
            home_2, gen_2, runtime=serving_root.runtime, observe=observe
        )

        backend_1 = _MultiMachineBackend(gen_1)
        backend_2 = _MultiMachineBackend(gen_2)

        with db_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO machine_units (machine_name, home, serve_gateway) "
                "VALUES ('shared-host', %s, true), ('shared-host', %s, false)",
                (str(home_1.resolve()), str(home_2.resolve())),
            )
        db_conn.commit()

        mgr_1 = ScheduleManager(
            pool,
            backend=backend_1,
            repo_root=REPO_ROOT,
            home=home_1,
            policy=POLICY,
            launch_env={},
            machine_name="shared-host",
            observe=observe,
        )
        mgr_2 = ScheduleManager(
            pool,
            backend=backend_2,
            repo_root=REPO_ROOT,
            home=home_2,
            policy=POLICY,
            launch_env={},
            machine_name="shared-host",
            observe=observe,
        )

        assert mgr_1.is_active_unit() is True
        assert mgr_2.is_active_unit() is False

        sid = _insert(db_conn, "shared-task", machine="shared-host", enabled=True)

        mgr_2.reconcile(now=NOW)
        assert backend_2.launched == []

        mgr_1.reconcile(now=NOW)
        assert backend_1.launched == [sid]

        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE machine_units SET stopped_at = now() WHERE machine_name = 'shared-host' AND home = %s",
                (str(home_1.resolve()),),
            )
        db_conn.commit()

        assert mgr_1.is_active_unit() is False
        assert mgr_2.is_active_unit() is True
    finally:
        pool.close()

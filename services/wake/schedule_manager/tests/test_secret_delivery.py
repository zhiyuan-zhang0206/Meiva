from __future__ import annotations

from pathlib import Path
from typing import Any

import psycopg
from psycopg_pool import ConnectionPool

from base.cluster import session_name
from services.wake.schedule_manager.tests.manager_support import NOW, schedule_manager
from services.wake.schedule_manager.tests.session_fakes import Backend


class RecordingBackend(Backend):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.envs: dict[str, dict[str, str]] = {}

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
        self.envs[name] = dict(env)
        return super().new_session(
            name, cmd, cwd, env=env, login_shell=login_shell, exec_cmd=exec_cmd
        )


def test_launch_delivers_resolved_secrets_in_env_not_argv(
    db_conn: psycopg.Connection, unit_home: Path
) -> None:
    row = db_conn.execute(
        "INSERT INTO schedules (name, script, command, enabled, desired_revision, machine, secret_env_vars) "
        "VALUES ('secret-collector', 'pass', 'python schedule.py', true, 1, 'gateway', ARRAY['CUSTOM_ENDPOINT_KEY', 'CUSTOM_DISCOVERY_TAG']) "
        "RETURNING id"
    ).fetchone()
    assert row is not None
    schedule_id = int(row[0])
    db_conn.commit()

    called_with: list[tuple[int, tuple[str, ...]]] = []

    def mock_secret_resolver(sched_id: int, secret_names: tuple[str, ...]) -> dict[str, str]:
        called_with.append((sched_id, secret_names))
        return {
            "CUSTOM_ENDPOINT_KEY": "val-123",
            "CUSTOM_DISCOVERY_TAG": "val-456",
        }

    backend = RecordingBackend()
    pool: ConnectionPool[psycopg.Connection] = ConnectionPool(
        db_conn.info.dsn, min_size=1, max_size=2, open=True
    )
    try:
        mgr = schedule_manager(
            pool,
            backend,
            repo_root=unit_home,
            machine_name="gateway",
            secret_env_resolver=mock_secret_resolver,
        )
        mgr._launch(schedule_id, NOW)
    finally:
        pool.close()

    assert len(called_with) == 1
    assert called_with[0] == (schedule_id, ("CUSTOM_ENDPOINT_KEY", "CUSTOM_DISCOVERY_TAG"))

    sname = session_name(f"schedule-{schedule_id}")
    assert sname in backend.envs
    delivered_env = backend.envs[sname]

    assert delivered_env["CUSTOM_ENDPOINT_KEY"] == "val-123"
    assert delivered_env["CUSTOM_DISCOVERY_TAG"] == "val-456"
    assert delivered_env["MEIVA_SCHEDULE_ID"] == str(schedule_id)

    launched_cmd = backend.commands[sname]
    assert "val-123" not in launched_cmd
    assert "val-456" not in launched_cmd

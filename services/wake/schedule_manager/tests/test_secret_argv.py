from __future__ import annotations

from pathlib import Path

import psycopg
from psycopg_pool import ConnectionPool

from base.sessions.backend import get_shell_backend
from base.sessions.env_forwarding import forward_env_dict
from base.sessions.pty.tests.pty_service import PtyServiceProcess
from base.sessions.pty.tests.pty_service import pty_service as pty_service
from base.sessions.pty.tests.pty_shells import wait_for
from base.sessions.pty.tests.secret_argv import (
    SECRET_VALUES,
    assert_service_tree_clean,
    service_tree_argvs,
)
from base.sessions.pty.tests.secret_argv import (
    secrets_in_creator_env as secrets_in_creator_env,
)
from services.wake.schedule_manager.tests.manager_support import NOW, schedule_manager


def test_schedule_launch(
    pty_service: PtyServiceProcess,
    secrets_in_creator_env: None,
    db_conn: psycopg.Connection,
    unit_home: Path,
) -> None:
    stub = unit_home / ".venv" / "bin" / "python"
    stub.parent.mkdir(parents=True)
    dump = unit_home / "runner-env.txt"
    stub.write_text(f"#!/bin/sh\nenv > {dump}.tmp && mv {dump}.tmp {dump}\nexec sleep 300\n")
    stub.chmod(0o755)

    row = db_conn.execute(
        "INSERT INTO schedules (name, script, command, enabled, desired_revision) "
        "VALUES ('argv-security-probe', 'pass', 'python schedule.py', true, 1) RETURNING id"
    ).fetchone()
    assert row is not None
    schedule_id = int(row[0])
    db_conn.commit()
    pool: ConnectionPool[psycopg.Connection] = ConnectionPool(
        db_conn.info.dsn, min_size=1, max_size=2, open=True
    )
    try:
        schedule_manager(
            pool, get_shell_backend(), repo_root=unit_home, launch_env=forward_env_dict()
        )._launch(schedule_id, NOW)
    finally:
        pool.close()

    assert wait_for(dump.exists), f"the runner never started:\n{pty_service.output()}"
    runner_env = dump.read_text()
    assert f"MEIVA_SCHEDULE_ID={schedule_id}\n" in runner_env, (
        "the schedule id rides the request env"
    )
    for secret in SECRET_VALUES:
        assert secret not in runner_env, f"{secret!r} reached the schedule's environment"
    assert_service_tree_clean(pty_service, label="ScheduleManager._launch")
    for argv in service_tree_argvs(pty_service):
        assert "MEIVA_SCHEDULE_ID" not in " ".join(argv), f"schedule id on argv: {argv!r}"

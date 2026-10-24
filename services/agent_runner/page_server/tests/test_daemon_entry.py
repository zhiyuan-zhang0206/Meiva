# meiva:note
# The page-server executable, started as a supervisor starts it, against the test cluster.

from __future__ import annotations

import sys
from pathlib import Path

import psycopg

from base.config import ConfigBoot, field_domain
from base.config.tests.configuration import snapshot_process_config
from services.agent_runner.page_server import resources
from services.agent_runner.tests.real_daemon import run_to_exit, running_daemon

_DAEMON = [sys.executable, "-m", "services.agent_runner.page_server.daemon"]


def test_the_daemon_serves_runs_both_loops_and_stops_cleanly_on_sigterm(
    unit_home: Path,
    db_conn: psycopg.Connection,
    process_config: ConfigBoot,
    db_url: str,
    redis_url: str,
) -> None:
    port = resources.endpoint(process_config, runtime=unit_home / "run").health_port
    pidfile = unit_home / "run" / "page_server.pid"

    with running_daemon(
        _DAEMON,
        home=unit_home,
        health_port=port,
        machine_name="page-server-entry",
        db_url=db_url,
        redis_url=redis_url,
    ) as daemon:
        report = daemon.healthz()
        applications = db_conn.execute(
            "SELECT DISTINCT application_name FROM pg_stat_activity "
            "WHERE application_name LIKE 'meiva:page_server:%'"
        ).fetchall()
        assert pidfile.exists()

        code = daemon.stop()

    assert (report["name"], sorted(report["loops"])) == (
        "page_server",
        ["dead_show_pages", "reconcile"],
    )
    assert len(applications) == 1, "the database factory and the gate are one owner"
    assert code == 0
    assert not pidfile.exists()
    assert "[page-server] interrupted, shutting down" in daemon.log()


def test_a_second_daemon_leaves_with_code_one_while_the_first_runs(
    unit_home: Path, process_config: ConfigBoot, db_url: str, redis_url: str
) -> None:
    port = resources.endpoint(process_config, runtime=unit_home / "run").health_port
    with running_daemon(
        _DAEMON,
        home=unit_home,
        health_port=port,
        machine_name="page-server-a",
        db_url=db_url,
        redis_url=redis_url,
    ):
        code, log = run_to_exit(
            _DAEMON,
            home=unit_home,
            machine_name="page-server-b",
            db_url=db_url,
            redis_url=redis_url,
        )

    assert code == 1
    assert "daemon already running" in log


def test_resources_follow_the_entry_owner_at_each_original_lookup(tmp_path: Path) -> None:
    config = snapshot_process_config()
    runtime = tmp_path / "owned-entry" / "run"
    domain = field_domain("page_server_health_port")
    fields = getattr(config.view, domain)
    fields.page_server_health_port = 26101

    first = resources.endpoint(config, runtime=runtime)
    fields.page_server_health_port = 26102
    second = resources.endpoint(config, runtime=runtime)

    assert (first.health_port, second.health_port) == (26101, 26102)
    assert first.pidfile == second.pidfile == runtime / "page_server.pid"
    assert runtime.is_dir()

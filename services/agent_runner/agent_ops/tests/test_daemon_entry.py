# meiva:note
# The ops executable, started as a supervisor starts it, against the test cluster.

from __future__ import annotations

import signal
import socket
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import psycopg

from base.config import ConfigBoot
from services.agent_runner.agent_ops import boot
from services.agent_runner.tests.real_daemon import run_to_exit, running_daemon

_DAEMON = [sys.executable, "-m", "services.agent_runner.agent_ops.daemon"]
# meiva:note
# The supervisor's stop window: the daemon must leave well inside it.
_STOP_WINDOW_S = 8.0


def test_the_daemon_serves_registers_and_stops_cleanly_on_sigterm(
    unit_home: Path,
    db_conn: psycopg.Connection,
    process_config: ConfigBoot,
    db_url: str,
    redis_url: str,
) -> None:
    runtime = unit_home / "run"
    port = boot.endpoint(process_config, runtime=runtime).health_port

    with running_daemon(
        _DAEMON,
        home=unit_home,
        health_port=port,
        machine_name="ops-entry",
        db_url=db_url,
        redis_url=redis_url,
    ) as daemon:
        report = daemon.healthz()
        registered = db_conn.execute("SELECT name FROM machines").fetchall()
        pidfile_exists = (runtime / "ops.pid").exists()
        unauthenticated = _post_ops_status(port)

        code = daemon.stop()

    assert (report["name"], report["status"], report["saturation"]) == ("ops", "ok", 0.0)
    assert [component["name"] for component in report["components"]] == ["loop", "ops"]
    assert ("ops-entry",) in registered
    assert pidfile_exists
    assert unauthenticated == 401, "/ops answers nothing to a caller without a bearer"
    assert code == 0
    assert not (runtime / "ops.pid").exists()


def _post_ops_status(port: int) -> int:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/ops",
        data=b'{"kind": "config_read", "payload": {}}',
        method="POST",
    )
    try:
        # meiva:note
        # See the directive.
        with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code


def test_a_second_daemon_leaves_with_code_one_while_the_first_runs(
    unit_home: Path, process_config: ConfigBoot, db_url: str, redis_url: str
) -> None:
    port = boot.endpoint(process_config, runtime=unit_home / "run").health_port
    with running_daemon(
        _DAEMON,
        home=unit_home,
        health_port=port,
        machine_name="ops-a",
        db_url=db_url,
        redis_url=redis_url,
    ):
        code, log = run_to_exit(
            _DAEMON, home=unit_home, machine_name="ops-b", db_url=db_url, redis_url=redis_url
        )

    assert code == 1
    assert "indicates another instance is alive" in log


def test_a_daemon_on_a_database_without_the_schema_leaves_with_code_one(
    unit_home: Path, db_conn: psycopg.Connection, db_url: str, redis_url: str
) -> None:
    db_conn.autocommit = True
    db_conn.execute("CREATE DATABASE ops_without_schema")
    empty_url = db_url.rsplit("/", 1)[0] + "/ops_without_schema"
    try:
        code, log = run_to_exit(
            _DAEMON,
            home=unit_home,
            machine_name="ops-bad-schema",
            db_url=empty_url,
            redis_url=redis_url,
        )
    finally:
        db_conn.execute("DROP DATABASE ops_without_schema WITH (FORCE)")

    assert code == 1
    assert "schema version mismatch on ops-server startup" in log
    assert not (unit_home / "run" / "ops.pid").exists(), "the failed start left its pidfile"


# meiva:note
# The health port is held by another listener, so the daemon cannot serve: the failure
# escapes `serve_ops_daemon` and still reaches the log, and the supervisor sees a code.
def test_a_crash_escaping_the_daemon_is_logged_with_its_traceback_and_leaves_a_nonzero_code(
    unit_home: Path, db_url: str, redis_url: str
) -> None:
    with socket.socket() as holder:
        holder.bind(("127.0.0.1", 0))
        holder.listen(1)
        port = holder.getsockname()[1]

        code, log = run_to_exit(
            _DAEMON,
            home=unit_home,
            machine_name="ops-crash",
            db_url=db_url,
            redis_url=redis_url,
            extra={"MEIVA_OPS_HEALTH_PORT": str(port)},
        )

    assert code == 1
    assert "daemon crashed" in log
    assert "address already in use" in log.lower()
    assert not (unit_home / "run" / "ops.pid").exists(), "the crashed start left its pidfile"


# meiva:note
# A second SIGTERM that lands while the first is draining must not skip the hard exit into
# interpreter teardown (task #5129): the process leaves inside the stop window with code 0.
def test_a_retried_stop_does_not_abort_the_bounded_exit(
    unit_home: Path, process_config: ConfigBoot, db_url: str, redis_url: str
) -> None:
    port = boot.endpoint(process_config, runtime=unit_home / "run").health_port
    workspace = unit_home / "workspaces" / "7"
    workspace.mkdir(parents=True)
    (workspace / "to_user.md").write_text("pending")

    with running_daemon(
        _DAEMON,
        home=unit_home,
        health_port=port,
        machine_name="ops-retried-stop",
        db_url=db_url,
        redis_url=redis_url,
    ) as daemon:
        began = time.monotonic()
        daemon.process.send_signal(signal.SIGTERM)
        daemon.process.send_signal(signal.SIGTERM)
        code = daemon.process.wait(timeout=_STOP_WINDOW_S + 5)
        elapsed = time.monotonic() - began

    assert code == 0, daemon.log()
    assert elapsed < _STOP_WINDOW_S, f"the stop took {elapsed:.2f}s"

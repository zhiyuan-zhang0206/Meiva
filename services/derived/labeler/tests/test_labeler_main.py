from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import psycopg
import pytest

from base.host.env import port_lease
from base.native_process.code_version import CodeVersion
from base.native_process.loaded_commit import LoadedCommit
from base.paths import meiva_home

_REPO_ROOT = Path(__file__).resolve().parents[4]
_OTLP_ENDPOINT = "MEIVA_TELEMETRY_OTLP_ENDPOINT"


# meiva:note
# One port this session holds, from the host-wide lease: the number is refused to
# every other live holder on the host, so two workers — or two runs of one shard on
# one machine — cannot be given it.
def _free_port() -> int:
    return port_lease.lease_ports(1)[0]


def _backend_names(db_conn: psycopg.Connection) -> set[str]:
    db_conn.rollback()
    rows = db_conn.execute(
        "SELECT DISTINCT application_name FROM pg_stat_activity WHERE application_name LIKE 'meiva:%%'"
    ).fetchall()
    return {row[0] for row in rows}


# meiva:note
# The production entry as a real process: it captures its commit, checks the schema, builds its
# logging and database from one gate, serves its health endpoint and polls the database, then
# leaves cleanly on SIGTERM. The child gets a home of its own, a free health port and a dead
# telemetry endpoint; the database and Redis are the test's throwaway ones, inherited through the
# environment.
def test_the_entry_serves_with_the_captured_commit_and_stops_on_sigterm(
    db_conn: psycopg.Connection, redis_url: str, tmp_path: Path
) -> None:
    home = tmp_path / "meiva-home"
    home.mkdir()
    port = _free_port()
    dead_telemetry = f"http://127.0.0.1:{_free_port()}"
    # meiva:note
    # The test cluster's own configuration (database, Redis, ports) plus this child's overrides.
    (home / ".env").write_text(
        (meiva_home() / ".env").read_text(encoding="utf-8")
        + f"\n{_OTLP_ENDPOINT}={dead_telemetry}\nMEIVA_MACHINE_NAME=labeler-entry-test\n"
        + f"MEIVA_LABELER_HEALTH_PORT={port}\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["MEIVA_HOME"] = str(home)
    env[_OTLP_ENDPOINT] = dead_telemetry
    env["MEIVA_LABELER_HEALTH_PORT"] = str(port)
    env.pop("MEIVA_PERMISSIONS_HELPER_PID", None)
    log_path = tmp_path / "labeler.log"
    expected_name = f"meiva:labeler:v{CodeVersion(LoadedCommit.capture()).get()}"
    with log_path.open("wb") as log:
        proc = subprocess.Popen(
            [sys.executable, "-m", "services.derived.labeler.daemon"],
            cwd=_REPO_ROOT,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        try:
            deadline = time.monotonic() + 90.0
            while time.monotonic() < deadline and proc.poll() is None:
                if expected_name in _backend_names(db_conn):
                    break
                time.sleep(0.1)
            else:
                pytest.fail(
                    f"the entry never connected as {expected_name!r}:\n{log_path.read_text()[-4000:]}"
                )
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=5.0) as health:
                assert health.status == 200
            assert (home / "run" / "labeler.pid").exists()

            proc.send_signal(signal.SIGTERM)
            exit_code = proc.wait(timeout=30.0)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5.0)

    assert exit_code == 0, log_path.read_text()[-4000:]
    assert "[labeler] interrupted, shutting down" in log_path.read_text()
    assert not (home / "run" / "labeler.pid").exists()

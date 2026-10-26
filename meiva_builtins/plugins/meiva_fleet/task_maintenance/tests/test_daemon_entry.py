# meiva:note
# The task-maintenance daemon over real resources: a pidfile and healthz port of its own under a
# temp directory, the throwaway Postgres and Redis the fixtures provision, a separate process that
# holds the pidfile the way a running daemon does, and the daemon's own `main()` run as a child
# process the way the supervisor runs it.
from __future__ import annotations

import asyncio
import http.client
import json
import os
import socket
import subprocess
import sys
from collections.abc import Iterator
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from time import monotonic, sleep
from typing import Any

import psycopg
import pytest

from base.clock.tests.seeded_rng import seeded_rng
from base.daemon.endpoints import ServiceEndpoint
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.host.env import port_lease
from base.host.env.registry import SERVICE_PLUGIN_CONFIG_ENV
from base.native_process.code_version import CodeVersion
from base.native_process.loaded_commit import LoadedCommit
from base.packages.plugin_config_images import config_image_path
from meiva_builtins.plugins.meiva_fleet.default_config import FleetConfig
from meiva_builtins.plugins.meiva_fleet.task_maintenance import daemon

_HOLDER = Path(__file__).with_name("pidfile_holder.py")
_REPO_ROOT = Path(__file__).resolve().parents[5]


# meiva:note
# One port this session holds, from the host-wide lease: the number is refused to
# every other live holder on the host, so two workers — or two runs of one shard on
# one machine — cannot be given it.
def _free_port() -> int:
    return port_lease.lease_ports(1)[0]


def _endpoint(directory: Path) -> ServiceEndpoint:
    return ServiceEndpoint("task_maintenance", _free_port(), directory / "task_maintenance.pid")


def _healthz_status(endpoint: ServiceEndpoint) -> int | None:
    connection = http.client.HTTPConnection("127.0.0.1", endpoint.health_port, timeout=2)
    try:
        connection.request("GET", "/healthz")
        return connection.getresponse().status
    except OSError:
        return None
    finally:
        connection.close()


async def _until_healthy(endpoint: ServiceEndpoint) -> None:
    deadline = monotonic() + 30
    while await asyncio.to_thread(_healthz_status, endpoint) != 200:
        assert monotonic() < deadline, "the daemon never reported healthy"
        await asyncio.sleep(0.1)


# meiva:note
# A real process whose argv names the daemon module, which is how the pidfile guard recognizes a
# running daemon.
@pytest.fixture
def running_daemon_process(tmp_path: Path) -> Iterator[subprocess.Popen[bytes]]:
    holder = subprocess.Popen(  # noqa: S603
        [sys.executable, str(_HOLDER), daemon.__name__]
    )
    try:
        yield holder
    finally:
        holder.kill()
        holder.wait()


def _sessions(db_conn: psycopg.Connection[Any], gate: ProcessDbGate) -> int:
    row = db_conn.execute(
        "SELECT count(*) FROM pg_stat_activity WHERE application_name = %s",
        (gate.application_name(),),
    ).fetchone()
    db_conn.commit()
    assert row is not None
    return int(row[0])


async def _until_sessions(
    db_conn: psycopg.Connection[Any], gate: ProcessDbGate, *, open_: bool
) -> None:
    deadline = monotonic() + 30
    while (_sessions(db_conn, gate) > 0) != open_:
        assert monotonic() < deadline, "the daemon's database sessions never settled"
        await asyncio.sleep(0.05)


def _assert_released(endpoint: ServiceEndpoint) -> None:
    assert not endpoint.pidfile.exists()
    with (
        pytest.raises(OSError),
        closing(socket.create_connection(("127.0.0.1", endpoint.health_port), 1)),
    ):
        pass


async def test_run_binds_pidfile_and_health_before_the_database_and_releases_all_when_stopped(
    tmp_path: Path,
    database: Database,
    database_gate: ProcessDbGate,
    event_bus: EventBus,
    db_conn: psycopg.Connection[Any],
) -> None:
    endpoint = _endpoint(tmp_path)

    def database_after_health() -> Database:
        assert endpoint.pidfile.read_text().strip() == str(os.getpid())
        with closing(socket.create_connection(("127.0.0.1", endpoint.health_port), 1)):
            pass
        return database

    serving = asyncio.create_task(
        daemon.run(
            FleetConfig(),
            database=database_after_health,
            bus=event_bus,
            image=LoadedCommit.capture(),
            endpoint=endpoint,
        )
    )
    try:
        await _until_healthy(endpoint)
        await _until_sessions(db_conn, database_gate, open_=True)
        assert endpoint.pidfile.read_text().strip() == str(os.getpid())
    finally:
        serving.cancel()
    with pytest.raises(asyncio.CancelledError):
        await serving

    _assert_released(endpoint)
    await _until_sessions(db_conn, database_gate, open_=False)


async def test_a_schema_drift_ends_the_run_with_the_error_and_releases_everything(
    tmp_path: Path,
    database: Database,
    database_gate: ProcessDbGate,
    event_bus: EventBus,
    db_conn: psycopg.Connection[Any],
) -> None:
    endpoint = _endpoint(tmp_path)
    db_conn.execute("ALTER TABLE agent_tasks RENAME TO agent_tasks_drifted")
    db_conn.commit()
    try:
        with pytest.raises(psycopg.errors.UndefinedTable):
            await asyncio.wait_for(
                daemon.run(
                    FleetConfig(),
                    database=lambda: database,
                    bus=event_bus,
                    image=LoadedCommit.capture(),
                    endpoint=endpoint,
                ),
                timeout=60,
            )
    finally:
        db_conn.rollback()
        db_conn.execute("ALTER TABLE agent_tasks_drifted RENAME TO agent_tasks")
        db_conn.commit()

    _assert_released(endpoint)
    await _until_sessions(db_conn, database_gate, open_=False)


def test_run_refuses_to_start_while_a_daemon_holds_the_pidfile(
    tmp_path: Path, running_daemon_process: subprocess.Popen[bytes]
) -> None:
    endpoint = _endpoint(tmp_path)
    endpoint.pidfile.write_text(f"{running_daemon_process.pid}\n", encoding="utf-8")

    def database() -> Database:
        raise AssertionError("a refused daemon must not open its database")

    with pytest.raises(SystemExit) as refused:
        asyncio.run(
            daemon.run(
                FleetConfig(),
                database=database,
                bus=EventBus.from_settings(rng=seeded_rng()),
                image=LoadedCommit.capture(),
                endpoint=endpoint,
            )
        )

    assert refused.value.code == 1
    assert endpoint.pidfile.read_text().strip() == str(running_daemon_process.pid)
    assert _healthz_status(endpoint) is None


def _fleet_packet(fleet: FleetConfig) -> str:
    return json.dumps({"plugin": "meiva_fleet", "config": fleet.model_dump()})


@dataclass(frozen=True)
class _DaemonProcess:
    endpoint: ServiceEndpoint
    process: subprocess.Popen[bytes]
    log: Path

    def output(self) -> str:
        return self.log.read_text(encoding="utf-8", errors="replace")


def _start_daemon(
    home: Path, db_url: str, redis_url: str, *, delivered: FleetConfig | None
) -> _DaemonProcess:
    endpoint = ServiceEndpoint(
        "task_maintenance", _free_port(), home / "run" / "task_maintenance.pid"
    )
    (home / ".env").write_text(
        f"MEIVA_MACHINE_NAME=fleet-entry\nMEIVA_DB_URL={db_url}\nMEIVA_REDIS_URL={redis_url}\n",
        encoding="utf-8",
    )
    env = {
        **os.environ,
        "MEIVA_HOME": str(home),
        "MEIVA_CONFIG_FETCH": "skip",
        "MEIVA_TASK_MAINTENANCE_HEALTH_PORT": str(endpoint.health_port),
    }
    env.pop(SERVICE_PLUGIN_CONFIG_ENV, None)
    if delivered is not None:
        env[SERVICE_PLUGIN_CONFIG_ENV] = _fleet_packet(delivered)
    log = home / "daemon.log"
    with log.open("wb") as sink:
        process = subprocess.Popen(
            [sys.executable, "-m", "meiva_builtins.plugins.meiva_fleet.task_maintenance.daemon"],
            cwd=_REPO_ROOT,
            env=env,
            stdout=sink,
            stderr=subprocess.STDOUT,
        )
    return _DaemonProcess(endpoint, process, log)


def _await_started(running: _DaemonProcess) -> str:
    deadline = monotonic() + 60
    while _healthz_status(running.endpoint) != 200 or "daemon started" not in running.output():
        assert running.process.poll() is None, running.output()
        assert monotonic() < deadline, running.output()
        sleep(0.1)
    return running.output()


def _stop(running: _DaemonProcess) -> int:
    running.process.terminate()
    try:
        return running.process.wait(timeout=30)
    finally:
        running.process.kill()
        running.process.wait()


def _lines_in_order(output: str, *fragments: str) -> None:
    positions = [output.find(fragment) for fragment in fragments]
    assert all(position >= 0 for position in positions), output
    assert positions == sorted(positions), output


def test_main_claims_its_pidfile_and_health_before_the_loop_with_the_delivered_fleet_config(
    tmp_path: Path, db_url: str, redis_url: str
) -> None:
    delivered = FleetConfig(
        task_maintenance_interval_seconds=123.0,
        task_reminder_backoff_seconds=456.0,
        task_escalate_n=7,
    )
    image = FleetConfig(
        task_maintenance_interval_seconds=11.0,
        task_reminder_backoff_seconds=22.0,
        task_escalate_n=5,
    )
    image_path = config_image_path("meiva_fleet", home=tmp_path)
    image_path.parent.mkdir(parents=True)
    image_path.write_text(image.model_dump_json(), encoding="utf-8")
    running = _start_daemon(tmp_path, db_url, redis_url, delivered=delivered)
    try:
        output = _await_started(running)
        assert running.endpoint.pidfile.read_text().strip() == str(running.process.pid)
    finally:
        exit_code = _stop(running)
    _lines_in_order(
        output,
        "service started: task_maintenance",
        "pidfile written",
        "healthz listening",
        "interval=123s, backoff=456s, escalate_n=7",
    )
    assert f"sha={LoadedCommit.capture().sha}" in output
    assert "host=fleet-entry" in output
    assert exit_code == 0, running.output()
    assert not running.endpoint.pidfile.exists()


def test_main_reads_the_disk_image_when_no_fleet_config_was_delivered(
    tmp_path: Path, db_url: str, redis_url: str
) -> None:
    image = FleetConfig(
        task_maintenance_interval_seconds=11.0,
        task_reminder_backoff_seconds=22.0,
        task_escalate_n=5,
    )
    image_path = config_image_path("meiva_fleet", home=tmp_path)
    image_path.parent.mkdir(parents=True)
    image_path.write_text(image.model_dump_json(), encoding="utf-8")
    running = _start_daemon(tmp_path, db_url, redis_url, delivered=None)
    try:
        output = _await_started(running)
    finally:
        _stop(running)
    assert "interval=11s, backoff=22s, escalate_n=5" in output, output


def test_main_opens_its_database_sessions_under_the_version_of_its_loaded_image(
    tmp_path: Path, db_url: str, redis_url: str, db_conn: psycopg.Connection[Any]
) -> None:
    running = _start_daemon(tmp_path, db_url, redis_url, delivered=FleetConfig())
    try:
        _await_started(running)
        names = {
            row[0]
            for row in db_conn.execute(
                "SELECT application_name FROM pg_stat_activity "
                "WHERE application_name LIKE 'meiva:task_maintenance:%'"
            ).fetchall()
        }
    finally:
        _stop(running)
    version = CodeVersion(LoadedCommit.capture()).get()
    assert names == {f"meiva:task_maintenance:v{version}"}


def test_main_checks_the_schema_before_it_claims_anything(tmp_path: Path, redis_url: str) -> None:
    running = _start_daemon(
        tmp_path, "postgresql://meiva:test@127.0.0.1:1/meiva", redis_url, delivered=FleetConfig()
    )
    try:
        assert running.process.wait(timeout=60) != 0
    finally:
        running.process.kill()
        running.process.wait()
    output = running.output()
    assert "assert_schema_current" in output, output
    assert "service started" not in output
    assert not running.endpoint.pidfile.exists()
    assert _healthz_status(running.endpoint) is None


def test_main_refuses_to_start_beside_a_running_daemon_and_leaves_its_pidfile(
    tmp_path: Path,
    db_url: str,
    redis_url: str,
    running_daemon_process: subprocess.Popen[bytes],
) -> None:
    pidfile = tmp_path / "run" / "task_maintenance.pid"
    pidfile.parent.mkdir()
    pidfile.write_text(f"{running_daemon_process.pid}\n", encoding="utf-8")
    running = _start_daemon(tmp_path, db_url, redis_url, delivered=FleetConfig())
    try:
        exit_code = running.process.wait(timeout=60)
    finally:
        running.process.kill()
        running.process.wait()
    assert exit_code == 1, running.output()
    assert "already running" in running.output()
    assert pidfile.read_text().strip() == str(running_daemon_process.pid)
    assert _healthz_status(running.endpoint) is None

# meiva:note
# The scheduler and its worker compose their database handle, health and cleanup from their owners.
#
# These are the invariants the process entries once asserted through stand-ins for the whole
# composition: one gate shared by every handle, a handle that reads its config owner when it is
# built, the entry image served by health, and a loop failure that still stops health and removes
# the pidfile. Here each is observed on a real connection, a real listener and real files.

from __future__ import annotations

import asyncio
import json
import socket
import time
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict

from base.cluster.machine import MachineIdentity
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.config import ConfigBoot
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.host.env import port_lease
from base.native_process.loaded_commit import LoadedCommit
from services.backup.layout import BackupTree
from services.backup.scheduler import daemon, worker
from services.backup.scheduler.settings import SchedulerSettings
from services.backup.scheduler.tests.operation_support import STUB_WORKER
from services.backup.scheduler.worker import WorkerLaunch


def _config(db_url: str) -> ConfigBoot:
    config = ConfigBoot()
    config.read_process_environment()
    config.view.data_plane.db_url = db_url
    return config


def _refused(db_url: str) -> str:
    parts = conninfo_to_dict(db_url)
    return f"postgresql://{parts['user']}@127.0.0.1:1/{parts['dbname']}"


def _application_name(database: Database) -> str:
    with database.connect() as conn:
        row = conn.execute("SHOW application_name").fetchone()
    assert row is not None
    return str(row[0])


def test_the_scheduler_log_database_carries_the_process_gate_and_reads_its_owner_when_built(
    db_url: str, database_gate: ProcessDbGate, machine_identity: MachineIdentity
) -> None:
    config = _config(db_url)

    first = daemon.scheduler_database(config, machine_identity, database_gate)

    assert _application_name(first) == database_gate.application_name()
    config.view.data_plane.db_url = _refused(db_url)
    rebuilt = daemon.scheduler_database(config, machine_identity, database_gate)
    with pytest.raises(psycopg.OperationalError):
        rebuilt.connect()


def test_every_worker_handle_shares_one_gate_and_the_scratch_url_replaces_only_the_dial(
    db_url: str, database_gate: ProcessDbGate, machine_identity: MachineIdentity
) -> None:
    config = _config(_refused(db_url))

    scratch = worker.worker_database(config, machine_identity, database_gate, url=db_url)

    assert _application_name(scratch) == database_gate.application_name()
    live = worker.worker_database(config, machine_identity, database_gate)
    with pytest.raises(psycopg.OperationalError):
        live.connect()


def test_a_scratch_handle_reads_the_owners_ssl_policy_when_it_is_built(
    db_url: str, database_gate: ProcessDbGate, machine_identity: MachineIdentity
) -> None:
    config = _config(db_url)
    config.view.data_plane.db_sslmode = "verify-full"

    strict = worker.worker_database(config, machine_identity, database_gate, url=db_url)

    with pytest.raises(psycopg.OperationalError):
        strict.connect()


# meiva:note
# One port this session holds, from the host-wide lease: the number is refused to
# every other live holder on the host, so two workers — or two runs of one shard on
# one machine — cannot be given it.
def _free_port() -> int:
    return port_lease.lease_ports(1)[0]


async def _healthz(port: int) -> tuple[int, dict[str, object]]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(b"GET /healthz HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
    await writer.drain()
    response = await reader.read()
    writer.close()
    await writer.wait_closed()
    headers, body = response.split(b"\r\n\r\n", 1)
    return int(headers.split()[1]), json.loads(body)


def _scheduler_config(port: int) -> ConfigBoot:
    config = ConfigBoot()
    config.view.services.pg_backup_health_port = port
    return config


def _settings(timezone: str = "UTC") -> SchedulerSettings:
    return SchedulerSettings(
        timezone=ZoneInfo(timezone), backup_hour=0, keep=7, throwaway_base=None
    )


def _launch(tmp_path: Path) -> WorkerLaunch:
    return WorkerLaunch(STUB_WORKER, {"STUB_MODE": "succeed", "STUB_LOG": str(tmp_path / "log")})


def _port_refuses(port: int) -> bool:
    with socket.socket() as probe:
        return probe.connect_ex(("127.0.0.1", port)) != 0


async def test_the_scheduler_serves_health_from_its_entry_image_and_cleans_up_when_stopped(
    tmp_path: Path,
) -> None:
    image = LoadedCommit(tmp_path, "captured-scheduler")
    port = _free_port()
    tree = BackupTree(tmp_path / "home")
    tree.managed.mkdir(parents=True)
    (tree.managed / f"stubdb-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.dump.enc").write_bytes(
        b"today's dump, so the loop only sleeps"
    )
    run_dir = tmp_path / "run"
    task = asyncio.create_task(
        daemon.run(
            config=_scheduler_config(port),
            image=image,
            runtime=run_dir,
            tree=tree,
            launch=_launch(tmp_path),
            settings=_settings(),
        )
    )
    deadline = time.monotonic() + 20
    while True:
        try:
            status, payload = await _healthz(port)
            break
        except OSError:
            assert time.monotonic() < deadline, "the health server never came up"
            await asyncio.sleep(0.05)

    assert status == 200
    assert payload["sha"] == "captured-scheduler"
    assert (run_dir / "pg_backup.pid").exists()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not (run_dir / "pg_backup.pid").exists()
    assert _port_refuses(port)


async def test_a_loop_failure_still_stops_health_removes_the_pidfile_and_surfaces_unchanged(
    tmp_path: Path,
) -> None:
    port = _free_port()
    tree = BackupTree(tmp_path / "home")
    tree.managed.parent.mkdir(parents=True)
    tree.managed.write_text("a file where the dump pool should be a directory")
    run_dir = tmp_path / "run"

    with pytest.raises(NotADirectoryError):
        await daemon.run(
            config=_scheduler_config(port),
            image=LoadedCommit(tmp_path, "captured-scheduler"),
            runtime=run_dir,
            tree=tree,
            launch=_launch(tmp_path),
            settings=_settings(),
        )

    assert not (run_dir / "pg_backup.pid").exists()
    assert _port_refuses(port)

from __future__ import annotations

# meiva:note
# Heartbeat and IM entries compose their database handle, health server, and cleanup from their owners.
import asyncio
import json
import random
import socket
import time
from collections.abc import Callable, Coroutine
from pathlib import Path

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict

from base.clock import SYSTEM_TIMERS, Clock, ClockConfig
from base.cluster.dataplane.direct_url import ClusterDirectUrl
from base.cluster.machine import MachineIdentityReader
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.config import ConfigBoot
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.db.config import db_config_from_boot
from base.host.env import port_lease
from base.native_process.loaded_commit import LoadedCommit
from base.paths import meiva_home
from services.entrypoints.im_bridge import daemon as im_bridge_daemon
from services.wake.heartbeat import daemon as heartbeat_daemon


def _config(db_url: str, *, service: str, port: int) -> ConfigBoot:
    config = ConfigBoot()
    config.read_process_environment()
    config.view.data_plane.db_url = db_url
    config.set_field(f"{service}_health_port", port)
    config.view.services.im_disabled_adapters = ["telegram", "weixin", "feishu"]
    return config


def _refused(db_url: str) -> str:
    parts = conninfo_to_dict(db_url)
    return f"postgresql://{parts['user']}@127.0.0.1:1/{parts['dbname']}"


def _application_name(database: Database) -> str:
    with database.connect() as conn:
        row = conn.execute("SHOW application_name").fetchone()
    assert row is not None
    return str(row[0])


def _free_port() -> int:
    return port_lease.lease_ports(1)[0]


def _port_refuses(port: int) -> bool:
    with socket.socket() as probe:
        return probe.connect_ex(("127.0.0.1", port)) != 0


async def _healthz(port: int) -> tuple[int, dict[str, object]]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(b"GET /healthz HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
    await writer.drain()
    response = await reader.read()
    writer.close()
    await writer.wait_closed()
    headers, body = response.split(b"\r\n\r\n", 1)
    return int(headers.split()[1]), json.loads(body)


def _start_entry(
    service: str,
    *,
    config: ConfigBoot,
    runtime: Path,
    database: Callable[[], Database],
    image: LoadedCommit,
    machine: MachineIdentityReader,
) -> Coroutine[object, object, None]:
    if service == "heartbeat":
        clock = Clock(
            ClockConfig(
                timezone="UTC",
                authoritative_timezone="UTC",
                message_timestamp_weekday=False,
            ),
            timers=SYSTEM_TIMERS,
        )
        return heartbeat_daemon.run(
            process_config=config,
            runtime=runtime,
            database=database,
            image=image,
            machine=machine,
            clock=clock,
        )
    return im_bridge_daemon.run(
        process_config=config,
        runtime=runtime,
        database=database,
        image=image,
        clock=Clock(
            ClockConfig(
                timezone="UTC",
                authoritative_timezone="UTC",
                message_timestamp_weekday=False,
            ),
            timers=SYSTEM_TIMERS,
        ),
        rng=random.SystemRandom(),
    )


@pytest.mark.parametrize("service", ["heartbeat", "im_bridge"])
async def test_entry_retains_one_gate_serves_health_and_cleans_up_when_stopped(
    service: str,
    db_url: str,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentityReader,
    tmp_path: Path,
) -> None:
    port = _free_port()
    config = _config(db_url, service=service, port=port)
    image = LoadedCommit(tmp_path, f"captured-{service}")
    runtime = tmp_path / "run"

    def database() -> Database:
        return Database(
            db_config_from_boot(config),
            gate=database_gate,
            direct_url=ClusterDirectUrl(machine_identity.host, home=meiva_home()),
        )

    assert _application_name(database()) == database_gate.application_name()

    task = asyncio.create_task(
        _start_entry(
            service,
            config=config,
            runtime=runtime,
            database=database,
            image=image,
            machine=machine_identity,
        )
    )
    deadline = time.monotonic() + 20.0
    while True:
        try:
            status, payload = await _healthz(port)
            break
        except OSError:
            assert time.monotonic() < deadline, "the health server never came up"
            await asyncio.sleep(0.05)

    assert status == 200
    assert payload["sha"] == f"captured-{service}"
    assert (runtime / f"{service}.pid").exists()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not (runtime / f"{service}.pid").exists()
    assert _port_refuses(port)

    config.view.data_plane.db_url = _refused(db_url)
    with pytest.raises(psycopg.OperationalError):
        database().connect()

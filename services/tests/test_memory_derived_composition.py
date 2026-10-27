from __future__ import annotations

# meiva:note
# Memory search and indexer entries compose their database handle, server, and cleanup from their owners.
import asyncio
import json
import socket
import threading
import time
from pathlib import Path
from typing import TypedDict

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict
from pydantic import SecretStr

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
from base.telemetry.delivery.receipts import DrainStatus
from base.telemetry.pipeline_build import build_pipeline
from services.derived.memory_indexer import daemon as memory_indexer_daemon
from services.derived.memory_indexer.config import MemoryIndexerConfig
from services.derived.memory_search import daemon as memory_search_daemon


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


class _HealthPayload(TypedDict, total=False):
    sha: str
    index: dict[str, object]


async def _healthz(port: int) -> tuple[int, _HealthPayload]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(b"GET /healthz HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
    await writer.drain()
    response = await reader.read()
    writer.close()
    await writer.wait_closed()
    headers, body = response.split(b"\r\n\r\n", 1)
    return int(headers.split()[1]), json.loads(body)


async def _wait_for_indexer_patrol(port: int, deadline: float) -> tuple[int, _HealthPayload]:
    while True:
        try:
            status, payload = await _healthz(port)
            index = payload.get("index")
            if index is not None and index.get("last_success_at") is not None:
                return status, payload
        except OSError:
            assert time.monotonic() < deadline, "memory_indexer never came up"
        assert time.monotonic() < deadline, "memory_indexer never completed first patrol"
        await asyncio.sleep(0.02)


def test_memory_entries_share_gate_with_pipeline_and_read_config_owner_when_built(
    db_url: str,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentityReader,
    tmp_path: Path,
) -> None:
    config = ConfigBoot()
    config.read_process_environment()
    config.view.data_plane.db_url = db_url

    def database() -> Database:
        return Database(
            db_config_from_boot(config),
            gate=database_gate,
            direct_url=ClusterDirectUrl(machine_identity.host, home=meiva_home()),
        )

    pipeline = build_pipeline(database=database, machine=machine_identity, home=tmp_path)
    try:
        assert _application_name(database()) == database_gate.application_name()
    finally:
        drain = pipeline.stop(timeout=2)
    assert drain.status is DrainStatus.COMPLETED

    config.view.data_plane.db_url = _refused(db_url)
    with pytest.raises(psycopg.OperationalError):
        database().connect()


async def test_memory_search_and_indexer_serve_and_clean_up_when_stopped(
    db_url: str,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentityReader,
    tmp_path: Path,
) -> None:
    search_port = _free_port()
    indexer_port = _free_port()
    runtime = tmp_path / "run"
    config = ConfigBoot()
    config.read_process_environment()
    config.view.data_plane.db_url = db_url
    config.view.services.memory_search_port = search_port
    config.view.services.memory_search_pidfile = runtime / "memory_search.pid"
    config.view.services.memory_search_data_dir = tmp_path / "search_data"
    config.view.services.memory_search_backend = "numpy"
    config.view.services.memory_search_uri = f"http://127.0.0.1:{search_port}"
    config.set_field("memory_indexer_health_port", indexer_port)
    config.set_field("gemini_api_key", SecretStr("test-key"))

    search_cfg = memory_search_daemon.memory_search_config(config=config)
    server = await memory_search_daemon.build_server(search_cfg)
    search_thread = threading.Thread(target=server.run, daemon=True)
    search_thread.start()
    try:
        deadline = time.monotonic() + 20.0
        while not server.started:
            assert time.monotonic() < deadline, "memory_search never came up"
            await asyncio.sleep(0.02)
        search_status, _ = await _healthz(search_port)
        assert search_status == 200

        def database() -> Database:
            return Database(
                db_config_from_boot(config),
                gate=database_gate,
                direct_url=ClusterDirectUrl(machine_identity.host, home=meiva_home()),
            )

        indexer_task = asyncio.create_task(
            memory_indexer_daemon.run(
                config=config,
                runtime=runtime,
                database=database,
                image=LoadedCommit(tmp_path, "captured-memory-indexer"),
                root=tmp_path / "memory",
                tuning=MemoryIndexerConfig(loop_interval_s=0.02),
                home=meiva_home(),
            )
        )
        try:
            status, payload = await _wait_for_indexer_patrol(indexer_port, deadline)
            assert status == 200
            assert payload.get("sha") == "captured-memory-indexer"
            assert (runtime / "memory_indexer.pid").exists()
        finally:
            indexer_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await indexer_task
    finally:
        server.should_exit = True
        await asyncio.to_thread(search_thread.join, 5.0)

    assert not (runtime / "memory_indexer.pid").exists()
    assert _port_refuses(indexer_port)

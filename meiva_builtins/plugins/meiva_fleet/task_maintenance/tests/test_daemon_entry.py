# meiva:note
# The task-maintenance daemon's run loop over real resources: a pidfile and healthz port of its own
# under a temp directory, the throwaway Postgres and Redis the fixtures provision, and a separate
# process that holds the pidfile the way a running daemon does.
from __future__ import annotations

import asyncio
import http.client
import os
import socket
import subprocess
import sys
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path
from time import monotonic

import pytest

from base.daemon.endpoints import ServiceEndpoint
from base.db import Database
from base.events.live.bus import EventBus
from base.native_process.loaded_commit import LoadedCommit
from meiva_builtins.plugins.meiva_fleet.default_config import FleetConfig
from meiva_builtins.plugins.meiva_fleet.task_maintenance import daemon

_HOLDER = Path(__file__).with_name("pidfile_holder.py")


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


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


async def test_run_serves_health_and_holds_its_pidfile_until_stopped(
    tmp_path: Path, database: Database, event_bus: EventBus
) -> None:
    endpoint = _endpoint(tmp_path)
    serving = asyncio.create_task(
        daemon.run(
            FleetConfig(),
            database=lambda: database,
            bus=event_bus,
            image=LoadedCommit.capture(),
            endpoint=endpoint,
        )
    )
    try:
        await _until_healthy(endpoint)
        assert endpoint.pidfile.read_text().strip() == str(os.getpid())
    finally:
        serving.cancel()
    with pytest.raises(asyncio.CancelledError):
        await serving

    assert not endpoint.pidfile.exists()
    with (
        pytest.raises(OSError),
        closing(socket.create_connection(("127.0.0.1", endpoint.health_port), 1)),
    ):
        pass


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
                bus=EventBus.from_settings(),
                image=LoadedCommit.capture(),
                endpoint=endpoint,
            )
        )

    assert refused.value.code == 1
    assert endpoint.pidfile.read_text().strip() == str(running_daemon_process.pid)
    assert _healthz_status(endpoint) is None

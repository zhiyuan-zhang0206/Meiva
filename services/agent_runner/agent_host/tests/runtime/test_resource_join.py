"""Actual resource join bounds return without certifying pending clients as closed."""

import asyncio
import os
import subprocess
import sys
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock
from uuid import uuid4

import httpx
import psycopg
import pytest

from base.agents.context.clients import ClientSet
from base.agents.sdk.tests.sampling_owners import gateway_sampling_owner
from base.clock import SYSTEM_TIMERS
from base.clock.tests.seeded_rng import seeded_rng
from base.cluster.machine import MachineIdentityReader
from base.config import ConfigBoot, get_field
from base.config.tests.configuration import data_plane_environment
from base.daemon.endpoints import ServiceEndpoints
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.host.net.tests.local_http import serve_json
from base.lm.catalog import ModelCatalog
from base.packages.composer_commands import CommandCatalog
from base.paths import meiva_home, run_dir
from meiva.sdk_surface.installation import Installation
from services.agent_runner.agent_host import daemon
from services.agent_runner.agent_host.host import AgentHost
from services.agent_runner.agent_host.tests.host_policy import (
    configured_machine,
    configured_policy,
    handoff_timestamps,
)


class _PoolRecorder:
    def __init__(
        self,
        events: list[str],
        label: str,
        *,
        worker_thread: Any = None,
        pidfile: Path | None = None,
    ) -> None:
        self._events = events
        self._label = label
        self._worker_thread = worker_thread
        self._pidfile = pidfile

    async def close(self) -> None:
        if self._worker_thread is not None:
            assert not self._worker_thread.is_alive()
        if self._pidfile is not None:
            assert self._pidfile.exists()
        self._events.append(self._label)


class _ClientRecorder:
    def __init__(self, events: list[str] | None = None, *, marker: Path | None = None) -> None:
        self._events = events
        self._marker = marker

    def close(self) -> None:
        if self._events is not None:
            self._events.append("clients")
        if self._marker is not None:
            self._marker.touch()


async def test_sampling_failure_is_collected_before_pools_and_pidfile_cleanup(
    model_installation: Installation, tmp_path: Path, unit_home: Path
) -> None:
    _ = unit_home
    with serve_json({}, status=500) as gateway:
        sampling = gateway_sampling_owner(gateway.url, tmp_path / ".env")
        sampling.read()
        assert gateway.arrived.wait(2)
        worker = sampling.worker
        assert worker is not None and worker.completed.wait(5)
    pidfile = (
        ServiceEndpoints.from_fields(get_field, runtime=run_dir(meiva_home()))
        .of("agent_host")
        .pidfile
    )
    pidfile.parent.mkdir(parents=True, exist_ok=True)
    pidfile.write_text("123\n")
    events: list[str] = []
    workload_pool = _PoolRecorder(
        events, "workload_pool", worker_thread=worker.thread, pidfile=pidfile
    )
    control_pool = _PoolRecorder(
        events, "control_pool", worker_thread=worker.thread, pidfile=pidfile
    )
    with pytest.raises(httpx.HTTPStatusError) as caught:
        await daemon.close_process_owners(
            None,
            cast(Any, workload_pool),
            cast(Any, control_pool),
            replace(model_installation, sampling=sampling),
            None,
            None,
            None,
            pidfile=pidfile,
        )
    assert sampling.error is not None
    assert isinstance(caught.value, httpx.HTTPStatusError) and caught.value is sampling.error[0]
    assert events == ["control_pool", "workload_pool"]
    assert not pidfile.exists()


async def test_original_error_joins_before_clients_and_pools_close(
    model_catalog: ModelCatalog, *, database_gate: ProcessDbGate
) -> None:
    events: list[str] = []
    clients = ClientSet()
    clients.get(lambda: _ClientRecorder(events))
    host = AgentHost(
        runtime_owner=uuid4(),
        handoff_stamping=handoff_timestamps(),
        home=meiva_home(),
        policy=configured_policy(),
        pool=cast(Any, MagicMock()),
        checkpointer=cast(Any, object()),
        graph=cast(Any, object()),
        machine=configured_machine(name="test-box"),
        catalog=model_catalog,
        bus=EventBus.from_settings(rng=seeded_rng(), timers=SYSTEM_TIMERS),
        db=Database.from_settings(gate=database_gate),
        clients=clients,
        commands=CommandCatalog.system(skills=list, home=meiva_home()),
    )
    scope = await host._resource_service.turn()
    failure = ValueError("original late resource error")
    host._resource_service.record_failure(scope, failure, name="original")
    with pytest.raises(ValueError) as observed:
        await host.aclose()
    assert observed.value is failure
    assert host.resources_joined
    await daemon.close_joined_host_pools(
        host,
        cast(Any, _PoolRecorder(events, "workload_pool")),
        cast(Any, _PoolRecorder(events, "control_pool")),
    )
    assert events == ["clients", "control_pool", "workload_pool"]


def exercise_unfinished_main() -> None:
    """Production main's existing hard exit must skip a still-owned async task."""

    async def run(
        *,
        config: ConfigBoot,
        database: Callable[[], Database],
        machine: MachineIdentityReader,
        home: Path,
        user_home: Path,
        disabled_entries: tuple[str, ...],
        clients: ClientSet,
        on_clients_owned: Callable[[], None],
    ) -> None:
        _ = (config, machine, user_home, disabled_entries)
        assert home == Path(os.environ["MEIVA_HOME"])
        on_clients_owned()
        marker = Path(os.environ["RESOURCE_JOIN_MARKER"])
        clients.get(lambda: _ClientRecorder(marker=marker.with_suffix(".clients")))
        host = AgentHost(
            runtime_owner=uuid4(),
            handoff_stamping=handoff_timestamps(),
            home=meiva_home(),
            policy=configured_policy(),
            pool=cast(Any, MagicMock()),
            checkpointer=cast(Any, object()),
            graph=cast(Any, object()),
            machine=configured_machine(name="resource-join-test"),
            catalog=cast(ModelCatalog, MagicMock()),
            bus=EventBus.from_settings(rng=seeded_rng(), timers=SYSTEM_TIMERS),
            db=database(),
            clients=clients,
            commands=CommandCatalog.system(skills=list, home=home),
        )
        service = host._resource_service
        await service.turn()
        entered = asyncio.Event()

        async def uncooperative() -> None:
            entered.set()
            while True:
                try:
                    await asyncio.Future()
                except asyncio.CancelledError:
                    continue

        service.watch(uncooperative(), name="uncooperative-resource")
        await entered.wait()
        try:
            await host.aclose(resource_deadline=asyncio.get_running_loop().time() + 0.02)
        finally:
            marker.write_text(str(host.resources_joined))
            await daemon.close_joined_host_pools(host, cast(Any, object()), cast(Any, object()))

    pytest.MonkeyPatch().setattr(daemon, "run", run)
    daemon.main()


def test_unfinished_actual_task_reaches_existing_hard_exit(
    tmp_path: Path, db_conn: psycopg.Connection
) -> None:
    _ = db_conn
    marker = tmp_path / "join.txt"
    home = tmp_path / "home"
    home.mkdir()
    env = data_plane_environment()
    (home / ".env").write_text(
        f"MEIVA_MACHINE_NAME=resource-join-test\n"
        f"MEIVA_DB_URL={env['MEIVA_DB_URL']}\n"
        f"MEIVA_REDIS_URL={env['MEIVA_REDIS_URL']}\n"
    )
    env.update(
        MEIVA_HOME=str(home),
        RESOURCE_JOIN_MARKER=str(marker),
        MEIVA_MACHINE_NAME="resource-join-test",
    )
    env.pop("MEIVA_PERMISSIONS_HELPER_PID", None)
    result = subprocess.run(  # noqa: S603 -- fixed interpreter and repository-owned helper
        [
            sys.executable,
            __file__,
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=8,
        check=False,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert marker.exists(), result.stdout + result.stderr
    assert marker.read_text() == "False"
    assert not marker.with_suffix(".clients").exists()
    assert "uncooperative-resource" in result.stderr
    assert "keeping pools open until hard exit" in result.stderr


if __name__ == "__main__":
    exercise_unfinished_main()

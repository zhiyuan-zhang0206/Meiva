"""Actual resource join bounds return without certifying pending clients as closed."""

import asyncio
import os
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock

import httpx
import pytest

from base.agents.context.clients import ClientSet
from base.agents.sdk.tests.sampling_owners import gateway_sampling_owner
from base.cluster.machine import MachineIdentity
from base.config import ConfigBoot
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.host.net.tests.local_http import serve_json
from base.lm.catalog import ModelCatalog
from base.packages.composer_commands import CommandCatalog
from base.paths import meiva_home
from meiva.sdk_surface.installation import Installation
from services.agent_runner.agent_host import daemon
from services.agent_runner.agent_host.host import AgentHost
from services.agent_runner.agent_host.tests.host_policy import (
    configured_machine,
    configured_policy,
    handoff_timestamps,
)


async def test_sampling_failure_is_collected_before_pools_and_pidfile_cleanup(
    monkeypatch: pytest.MonkeyPatch, model_installation: Installation, tmp_path: Path
) -> None:
    with serve_json({}, status=500) as gateway:
        sampling = gateway_sampling_owner(gateway.url, tmp_path / ".env")
        sampling.read()
        assert gateway.arrived.wait(2)
        worker = sampling.worker
        assert worker is not None and worker.completed.wait(5)
    events: list[str] = []

    async def close_pools(*_args: object) -> None:
        assert not worker.thread.is_alive()
        events.append("pools")

    monkeypatch.setattr(daemon, "close_host_pools", close_pools)

    def remove_pidfile(_path: object) -> None:
        events.append("pidfile")

    monkeypatch.setattr(daemon, "remove_pidfile", remove_pidfile)
    with pytest.raises(httpx.HTTPStatusError) as caught:
        await daemon.close_process_owners(
            None,
            cast(Any, object()),
            cast(Any, object()),
            replace(model_installation, sampling=sampling),
            None,
            None,
            None,
        )
    assert sampling.error is not None
    assert isinstance(caught.value, httpx.HTTPStatusError) and caught.value is sampling.error[0]
    assert events == ["pools", "pidfile"]


async def test_original_error_joins_before_clients_and_pools_close(
    monkeypatch: pytest.MonkeyPatch, model_catalog: ModelCatalog, *, database_gate: ProcessDbGate
) -> None:
    host = AgentHost(
        handoff_stamping=handoff_timestamps(),
        home=meiva_home(),
        policy=configured_policy(),
        pool=cast(Any, MagicMock()),
        checkpointer=cast(Any, object()),
        graph=cast(Any, object()),
        machine=configured_machine(name="test-box"),
        catalog=model_catalog,
        bus=EventBus.from_settings(),
        db=Database.from_settings(gate=database_gate),
        commands=CommandCatalog.system(skills=list),
    )
    scope = await host._resource_service.turn()
    failure = ValueError("original late resource error")
    host._resource_service.record_failure(scope, failure, name="original")
    events: list[str] = []
    monkeypatch.setattr(host._clients, "close", lambda: events.append("clients"))

    async def close_pools(*_args: object) -> None:
        events.append("pools")

    monkeypatch.setattr(daemon, "close_host_pools", close_pools)
    with pytest.raises(ValueError) as observed:
        await host.aclose()
    assert observed.value is failure
    assert host.resources_joined
    await daemon.close_joined_host_pools(host, cast(Any, object()), cast(Any, object()))
    assert events == ["clients", "pools"]


def exercise_unfinished_main() -> None:
    """Production main's existing hard exit must skip a still-owned async task."""
    from base.deploy.schema import migrations

    def schema_gate(*_args: object) -> None:
        pass

    migrations.assert_schema_current = schema_gate

    async def run(
        *,
        config: ConfigBoot,
        database: Callable[[], Database],
        machine: MachineIdentity,
        home: Path,
        clients: ClientSet,
        on_clients_owned: Callable[[], None],
    ) -> None:
        assert home == Path(os.environ["MEIVA_HOME"])
        on_clients_owned()
        host = AgentHost(
            handoff_stamping=handoff_timestamps(),
            home=meiva_home(),
            policy=configured_policy(),
            pool=cast(Any, MagicMock()),
            checkpointer=cast(Any, object()),
            graph=cast(Any, object()),
            machine=configured_machine(name="resource-join-test"),
            catalog=cast(ModelCatalog, MagicMock()),
            bus=EventBus.from_settings(),
            db=database(),
            clients=clients,
            commands=CommandCatalog.system(skills=list),
        )
        marker = Path(os.environ["RESOURCE_JOIN_MARKER"])
        patch = pytest.MonkeyPatch()
        patch.setattr(host._clients, "close", lambda: marker.with_suffix(".clients").touch())
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


def test_unfinished_actual_task_reaches_existing_hard_exit(tmp_path: Path) -> None:
    marker = tmp_path / "join.txt"
    home = tmp_path / "home"
    home.mkdir()
    (home / ".env").write_text("MEIVA_MACHINE_NAME=resource-join-test\n")
    env = os.environ.copy()
    env.update(
        MEIVA_HOME=str(home),
        RESOURCE_JOIN_MARKER=str(marker),
        MEIVA_MACHINE_NAME="resource-join-test",
    )
    env.pop("MEIVA_PERMISSIONS_HELPER_PID", None)
    started = time.monotonic()
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
    assert time.monotonic() - started < 8
    assert marker.exists(), result.stdout + result.stderr
    assert marker.read_text() == "False"
    assert not marker.with_suffix(".clients").exists()
    assert "uncooperative-resource" in result.stderr
    assert "keeping pools open until hard exit" in result.stderr


if __name__ == "__main__":
    exercise_unfinished_main()

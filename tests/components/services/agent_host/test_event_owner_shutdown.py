from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock

import pytest

from base.agents.context.clients import ClientSet
from base.cluster.machine import MachineFacts
from base.cluster.machine.configured import ConfiguredMachineIdentity
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.config import ConfigBoot
from base.daemon.endpoints import ServiceEndpoints
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.lm.catalog import ModelCatalog
from base.packages.composer_commands import CommandCatalog
from base.paths import meiva_home
from base.paths.tests.homes import unit_home as unit_home
from base.telemetry.delivery.pipeline import EventPipeline
from base.telemetry.delivery.receipts import Event
from services.agent_runner.agent_host import daemon
from services.agent_runner.agent_host.host import AgentHost
from services.agent_runner.agent_host.tests.host_policy import configured_policy, handoff_timestamps


def event() -> Event:
    return Event(
        ts=datetime.now(UTC),
        trace_id=None,
        span_id=None,
        agent_id=None,
        machine="test",
        cluster="test",
        process="test",
        category="telemetry",
        event_name="sdk_call",
        level="info",
        source="system",
        target_agent_id=None,
    )


class _RecordingPool:
    def __init__(self, name: str, closed: list[str]) -> None:
        self.name = name
        self._closed = closed

    async def close(self) -> None:
        self._closed.append(self.name)


class _PoolFactoryDb:
    def __init__(self, closed: list[str]) -> None:
        self._closed = closed

    def async_pool(self, _cls: object, *, pool_name: str, **_kwargs: object) -> _RecordingPool:
        return _RecordingPool(pool_name, self._closed)


@pytest.mark.parametrize("native_failure", [False, True])
async def test_host_release_runs_and_first_original_failure_remains_primary(
    model_catalog: ModelCatalog,
    native_failure: bool,
    database_gate: ProcessDbGate,
    machine_identity: ConfiguredMachineIdentity,
) -> None:
    machine_identity.set(name="test-box")
    writer_error = TypeError("original event writer failed")
    resource_error = ValueError("original joined resource failed")

    def writer(batch: list[Event]) -> None:
        raise writer_error

    pipe = EventPipeline(writer=writer, batch_size=1)
    clients = ClientSet(pipeline_factory=lambda: pipe)
    host = AgentHost(
        handoff_stamping=handoff_timestamps(),
        home=meiva_home(),
        policy=configured_policy(),
        pool=cast(Any, MagicMock()),
        checkpointer=cast(Any, object()),
        graph=cast(Any, object()),
        machine=machine_identity,
        catalog=model_catalog,
        clients=clients,
        bus=EventBus.from_settings(),
        db=Database.from_settings(gate=database_gate),
        commands=CommandCatalog.system(skills=list),
    )
    try:
        clients.event_pipeline().enqueue(event())
        with pytest.raises(TypeError) as writer_observed:
            clients.sync_events(timeout=1)
        assert writer_observed.value is writer_error
        if native_failure:
            scope = await host._resource_service.turn()
            host._resource_service.record_failure(scope, resource_error, name="native")
        with pytest.raises((TypeError, ValueError)) as observed:
            await host.aclose()
        assert observed.value is (resource_error if native_failure else writer_error)
        assert host.resources_joined
        assert clients.event_pipeline() is pipe
        if native_failure:
            assert any(str(writer_error) in note for note in resource_error.__notes__)
    finally:
        with pytest.raises(TypeError):
            pipe.stop(timeout=1)


async def test_boot_before_host_closes_writer_even_if_it_failed_and_still_closes_pools(
    unit_home: Path,
) -> None:
    del unit_home
    original = TypeError("boot writer failed")

    def writer(batch: list[Event]) -> None:
        raise original

    pipe = EventPipeline(writer=writer, batch_size=1)
    clients = ClientSet(pipeline_factory=lambda: pipe)
    closed_pools: list[str] = []
    pidfile = ServiceEndpoints.from_settings().of("agent_host").pidfile
    pidfile.parent.mkdir(parents=True, exist_ok=True)
    pidfile.write_text("999999999\n", encoding="utf-8")
    assert not daemon._is_running()
    try:
        clients.event_pipeline().enqueue(event())
        with pytest.raises(TypeError):
            clients.sync_events(timeout=1)
        with pytest.raises(TypeError) as observed:
            await daemon.close_process_owners(
                None,
                cast(Any, _RecordingPool("agent-host", closed_pools)),
                cast(Any, _RecordingPool("agent-host-control", closed_pools)),
                None,
                None,
                None,
                None,
                clients=clients,
            )
        assert observed.value is original
        assert closed_pools == ["agent-host-control", "agent-host"]
        assert not pidfile.exists()
        assert clients.event_pipeline() is pipe
    finally:
        with pytest.raises(TypeError):
            pipe.stop(timeout=1)


async def test_boot_primary_survives_a_failed_constructed_writer_cleanup(
    unit_home: Path,
) -> None:
    boot_error = ValueError("original plugin bootstrap defect")
    writer_error = TypeError("boot event writer defect")
    closed_pools: list[str] = []
    pidfile = ServiceEndpoints.from_settings().of("agent_host").pidfile

    def writer(batch: list[Event]) -> None:
        raise writer_error

    pipe = EventPipeline(writer=writer, batch_size=1)
    clients = ClientSet(pipeline_factory=lambda: pipe)

    def fail_during_boot() -> None:
        assert pidfile.exists()
        clients.event_pipeline().enqueue(event())
        with pytest.raises(TypeError):
            clients.sync_events(timeout=1)
        raise boot_error

    machine = MachineFacts.of(
        name="test-box",
        role=frozenset({"agent-runner"}),
        host="localhost",
        description="test host",
    )
    config = ConfigBoot()
    config.read_process_environment()
    try:
        with pytest.raises(ValueError) as observed:
            await daemon.run(
                config=config,
                database=lambda: cast(Database, _PoolFactoryDb(closed_pools)),
                machine=machine,
                home=unit_home,
                clients=clients,
                on_clients_owned=fail_during_boot,
            )
        assert observed.value is boot_error
        assert any(str(writer_error) in note for note in boot_error.__notes__)
        assert closed_pools == ["agent-host-control", "agent-host"]
        assert not pidfile.exists()
    finally:
        with pytest.raises(TypeError):
            pipe.stop(timeout=1)

# meiva:link meiva/tests/fixtures/docs/sdk-composition.meiva.okf.md

from __future__ import annotations

import os
from collections.abc import AsyncGenerator, Callable
from pathlib import Path
from random import SystemRandom

import pytest

import meiva
from base.agents.context import MeivaContext
from base.agents.context.clients import ClientSet, DatabaseFactory
from base.agents.context.identity import AgentIdentity, ExternalLease
from base.api_contracts.agent_contract import Harness
from base.clock import SYSTEM_TIMERS, Clock
from base.clock.configuration import clock_config_from_boot
from base.cluster.machine import MachineIdentityReader
from base.cluster.machine.configured import ConfiguredMachineIdentity
from base.config.tests.configuration import snapshot_process_config
from base.events.live.bus import EventBus
from base.native_process.runtime_incarnation import RuntimeIncarnation
from base.native_process.turn_identity import HostedServiceResources, HostedTurnResources
from base.paths.home import resolve_meiva_home
from meiva.boot import clients_over, derived_clients
from meiva.sdk_surface import process_context


# TEMPORARY DEBUG PROBE (#6424) — debug/6424-dburl-probe only, never merge.
def _probe_snapshot(config: object) -> None:
    import sys

    view = getattr(config, "view", None)
    plane = getattr(view, "data_plane", None)
    url = getattr(plane, "db_url", "") or ""
    if ":1/" in url and "meiva_citest" in url:
        sys.stderr.write(
            "PROBE-SNAPSHOT test=%r url=%r env=%r\n"
            % (os.environ.get("PYTEST_CURRENT_TEST"), url, os.environ.get("MEIVA_DB_URL"))
        )
        sys.stderr.flush()


def _probe_tag(clients: ClientSet) -> None:
    if getattr(clients, "_probe_created_by", None) is not None:
        return
    try:
        clients._probe_created_by = os.environ.get("PYTEST_CURRENT_TEST")
    except Exception:
        pass


@pytest.fixture
async def hosted_resources() -> AsyncGenerator[HostedTurnResources]:
    """An explicit test service owns watcher/completion results until fixture join."""
    service = HostedServiceResources()
    try:
        yield await service.turn()
    finally:
        await service.aclose()


# meiva:note
# Clients over a snapshot of the process configuration, for a pinned agent whose test supplied none.
def snapshot_clients() -> ClientSet:
    config = snapshot_process_config()
    _probe_snapshot(config)
    clients = derived_clients(config, ConfiguredMachineIdentity(config))
    _probe_tag(clients)
    return clients


# meiva:note
# Clients over a snapshot of the process configuration whose database is the factory the test holds.
def snapshot_clients_over(database: DatabaseFactory) -> ClientSet:
    config = snapshot_process_config()
    _probe_snapshot(config)
    clients = clients_over(config, ConfiguredMachineIdentity(config), database)
    _probe_tag(clients)
    return clients


def pin_agent(
    agent_id: int | None,
    *,
    owns_loop: bool = True,
    actor: str | None = None,
    lease: ExternalLease | None = None,
    incarnation: RuntimeIncarnation | None = None,
    clock_factory: Callable[[], Clock] | None = None,
    clients: ClientSet | None = None,
    machine: MachineIdentityReader | None = None,
    harness: Harness = Harness.MEIVA,
    bus: EventBus | None = None,
    meiva_home: Path | None = None,
) -> None:
    bound = getattr(meiva, "context", None)
    # TEMPORARY DEBUG PROBE (#6424) — debug/6424-dburl-probe only, never merge.
    _probe_target = clients if clients is not None else (bound.clients if bound else None)
    if _probe_target is not None:
        _probe_tag(_probe_target)
    process_context.bind_context(
        MeivaContext(
            meiva_home=resolve_meiva_home() if meiva_home is None else meiva_home,
            identity=AgentIdentity(
                agent_id=agent_id, owns_loop=owns_loop, actor=actor, lease=lease
            ),
            harness=harness,
            original_incarnation=incarnation,
            machine=machine if machine is not None else (None if bound is None else bound.machine),
            clients=clients
            if clients is not None
            else (bound.clients if bound else snapshot_clients()),
            clock_factory=(
                clock_factory
                if clock_factory is not None
                else (None if bound is None else bound.clock_factory)
            ),
            rng=SystemRandom() if bound is None or bound.rng is None else bound.rng,
            bus=bus if bus is not None else (None if bound is None else bound.bus),
            user_home=Path.home(),
            environ=os.environ if bound is None or bound.environ is None else bound.environ,
        )
    )


def pin_no_identity() -> None:
    process_context.unbind_context()


def exec_context(
    agent_id: int | None,
    *,
    actor: str | None = None,
    incarnation: RuntimeIncarnation | None = None,
    resources: HostedTurnResources | None = None,
) -> MeivaContext:
    config = snapshot_process_config()
    machine = ConfiguredMachineIdentity(config)
    return MeivaContext(
        meiva_home=resolve_meiva_home(),
        identity=AgentIdentity(agent_id=agent_id, owns_loop=True, actor=actor),
        original_incarnation=incarnation,
        machine=machine,
        hosted_resources=resources,
        clients=derived_clients(config, machine),
        clock_factory=lambda: Clock(clock_config_from_boot(config), timers=SYSTEM_TIMERS),
        user_home=Path.home(),
        environ=os.environ,
    )

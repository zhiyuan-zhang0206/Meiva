# meiva:link meiva/tests/fixtures/docs/sdk-composition.meiva.okf.md

from __future__ import annotations

from collections.abc import AsyncGenerator, Callable

import pytest

import meiva
from base.agents.context import MeivaContext
from base.agents.context.clients import ClientSet, DatabaseFactory
from base.agents.context.identity import AgentIdentity, ExternalLease
from base.api_contracts.agent_contract import Harness
from base.clock import SYSTEM_TIMERS, Clock
from base.clock.configuration import clock_config_from_boot
from base.cluster.machine import MachineIdentity
from base.cluster.machine.boot import machine_from_boot
from base.config.tests.configuration import snapshot_process_config
from base.native_process.runtime_incarnation import RuntimeIncarnation
from base.native_process.turn_identity import HostedServiceResources, HostedTurnResources
from meiva.boot import clients_over, derived_clients
from meiva.sdk_surface import process_context


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
    return derived_clients(config, machine_from_boot(config))


# meiva:note
# Clients over a snapshot of the process configuration whose database is the factory the test holds.
def snapshot_clients_over(database: DatabaseFactory) -> ClientSet:
    config = snapshot_process_config()
    return clients_over(config, machine_from_boot(config), database)


def pin_agent(
    agent_id: int | None,
    *,
    owns_loop: bool = True,
    actor: str | None = None,
    lease: ExternalLease | None = None,
    incarnation: RuntimeIncarnation | None = None,
    clock_factory: Callable[[], Clock] | None = None,
    clients: ClientSet | None = None,
    machine: MachineIdentity | None = None,
    harness: Harness = Harness.MEIVA,
) -> None:
    bound = getattr(meiva, "context", None)
    process_context.bind_context(
        MeivaContext(
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
    machine = machine_from_boot(config)
    return MeivaContext(
        identity=AgentIdentity(agent_id=agent_id, owns_loop=True, actor=actor),
        original_incarnation=incarnation,
        machine=machine,
        hosted_resources=resources,
        clients=derived_clients(config, machine),
        clock_factory=lambda: Clock(clock_config_from_boot(config), timers=SYSTEM_TIMERS),
    )

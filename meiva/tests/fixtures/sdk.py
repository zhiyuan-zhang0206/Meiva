# meiva:link meiva/tests/fixtures/docs/sdk-composition.meiva.okf.md

from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest

import meiva
from base.agents.context import MeivaContext
from base.agents.context.identity import AgentIdentity
from base.clock import SYSTEM_TIMERS, Clock
from base.clock.configuration import clock_config_from_boot
from base.cluster.machine import MachineIdentityReader
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.config import ConfigBoot
from base.events.live.bus import EventBus
from base.paths.home import resolve_meiva_home
from meiva.boot import derived_clients
from meiva.sdk_surface import metering, process_context
from meiva.sdk_surface.installation import installed
from meiva.tests.fixtures.pin_agent import pin_agent

__all__ = ["pinned_bus", "sdk_environment", "sdk_identity", "sdk_metering"]


@pytest.fixture
def sdk_environment(
    process_config: ConfigBoot, machine_identity: MachineIdentityReader
) -> Iterator[MeivaContext]:
    """Own one SDK root for this test; repinning borrows its clients and machine.

    The prepared configuration remains independent of the test's bare home.
    Identity and metering guards finish before these clients are closed.
    """
    try:
        previous = meiva.context
    except AttributeError:
        previous = None
    context = MeivaContext(
        identity=AgentIdentity(agent_id=1, owns_loop=True),
        clients=derived_clients(process_config, machine_identity),
        machine=machine_identity,
        clock_factory=lambda: Clock(clock_config_from_boot(process_config), timers=SYSTEM_TIMERS),
        user_home=Path.home(),
        meiva_home=resolve_meiva_home(),
    )
    process_context.bind_context(context)
    try:
        yield context
    finally:
        try:
            context.clients.close()
        finally:
            if previous is None:
                process_context.unbind_context()
            else:
                process_context.bind_context(previous)


@pytest.fixture
def sdk_identity(sdk_environment: MeivaContext) -> ModuleType:
    """Supply the real context slot to the global identity restoration window."""
    return meiva


@pytest.fixture
def sdk_metering(sdk_environment: MeivaContext) -> Iterator[None]:
    """Remove only the installation's recorded outermost usage recorders."""
    yield
    current = installed()
    if current is not None and current.metered:
        metering.uninstall(current.metered)


# meiva:note
# The event bus of a test that publishes through the SDK: the `event_bus` fixture's, built as a
# process root builds it over the live settings (so over the test's Redis), drawing its retry
# jitter from a seeded generator. It binds a context that carries the bus, so every `pin_agent`
# after it inherits the bus as it inherits the clock. Only a test that publishes asks for it: the
# contexts the other fixtures build carry no bus, so a root that forgets one still fails its test.
@pytest.fixture
def pinned_bus(event_bus: EventBus) -> Iterator[EventBus]:
    try:
        previous = meiva.context
    except AttributeError:
        previous = None
    pin_agent(None, bus=event_bus)
    try:
        yield event_bus
    finally:
        if previous is None:
            process_context.unbind_context()
        else:
            process_context.bind_context(previous)

from collections.abc import Callable, Iterator
from dataclasses import replace
from pathlib import Path

import pytest

from base.agents.context import MeivaContext
from base.agents.context.clients import ClientSet
from base.clock import Clock
from base.clock.tests.fakes import system_clock
from base.clock.tests.seeded_rng import seeded_rng
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.paths.tests.homes import unit_home as unit_home
from base.sessions.pty.tests.pty_service import pty_service as pty_service
from meiva.sdk_surface import process_context
from meiva.sdk_surface.tests.agent_binding import BindAgent, agent_binder
from meiva.sdk_surface.tests.agent_binding import no_agent as no_agent
from meiva.shell.coding_tools.tests.fake_tools import short_dir as short_dir
from meiva.shell.tests.live_shell import live_host as live_host
from meiva.shell.tests.live_shell import live_shell as live_shell


# meiva:note
# The context a coding tool reads its home, its instant and its generator from: the test's home, a
# real clock and a seeded generator.
def _roots_context(home: Path) -> MeivaContext:
    return MeivaContext(meiva_home=home, clock_factory=system_clock, rng=seeded_rng())


# meiva:note
# Bind that context for a test that never acts as an agent (a status, a cancel, a capability report).
@pytest.fixture(autouse=True)
def coding_tools_context(unit_home: Path) -> Iterator[None]:
    previous = process_context.unbind_context()
    process_context.bind_context(_roots_context(unit_home))
    try:
        yield
    finally:
        process_context.unbind_context()
        if previous is not None:
            process_context.bind_context(previous)


# meiva:note
# `bound_agent` with the roots a coding tool reads put back: the shared binder unbinds the context at
# its setup and binds a bare one per call, so this fixture binds the roots context again before the
# test body runs (a launch can precede the agent binding) and completes every context a call binds.
@pytest.fixture
def bound_agent(unit_home: Path) -> Iterator[BindAgent]:
    rng = seeded_rng()
    with agent_binder() as shared:
        process_context.bind_context(_roots_context(unit_home))

        def bind(
            agent_id: int,
            *,
            clients: ClientSet | None = None,
            clock_factory: Callable[[], Clock] | None = None,
        ) -> None:
            shared(agent_id, clients=clients, clock_factory=clock_factory or system_clock)
            process_context.bind_context(
                replace(process_context.current_context(), meiva_home=unit_home, rng=rng)
            )

        yield bind

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from base.agents.context import MeivaContext, deferred_event_bus
from base.clock import SYSTEM_TIMERS
from base.clock.tests.seeded_rng import seeded_rng
from base.config.tests.configuration import snapshot_process_config
from base.events.live.bus import EventBus, EventBusConfig


def _bus() -> EventBus:
    return EventBus(
        EventBusConfig(redis_url="redis://127.0.0.1:1/0", events_channel="unit"),
        rng=seeded_rng(),
        timers=SYSTEM_TIMERS,
    )


def test_a_context_without_a_bus_or_a_factory_refuses_to_read_one() -> None:
    with pytest.raises(RuntimeError, match="no EventBus"):
        MeivaContext().require_bus()


def test_a_context_hands_back_the_bus_its_root_recorded() -> None:
    bus = _bus()

    assert MeivaContext(bus=bus).require_bus() is bus


def test_a_recorded_bus_is_read_before_the_factory_is_asked() -> None:
    bus = _bus()

    def refuse() -> EventBus:
        raise AssertionError("the factory must not be asked when the context holds a bus")

    assert MeivaContext(bus=bus, bus_factory=refuse).require_bus() is bus


def test_a_factory_is_asked_when_the_bus_is_first_read_not_when_the_context_is_built() -> None:
    asked: list[int] = []
    bus = _bus()

    def factory() -> EventBus:
        asked.append(1)
        return bus

    context = MeivaContext(bus_factory=factory)

    assert asked == []
    assert context.require_bus() is bus
    assert asked == [1]


def test_a_deferred_bus_is_one_bus_over_the_roots_data_plane_configuration() -> None:
    config = snapshot_process_config()
    context = MeivaContext(
        bus_factory=deferred_event_bus(config, rng=seeded_rng(), timers=SYSTEM_TIMERS)
    )

    bus = context.require_bus()

    assert isinstance(bus, EventBus)
    assert context.require_bus() is bus
    assert bus.channel == config.view.data_plane.events_channel


# meiva:note
# Only a fresh interpreter shows what an import pulls in, so the check runs the child file with
# the test's own interpreter and a fixed argument list.
def test_a_deferred_bus_imports_the_redis_stack_only_when_first_read() -> None:
    proc = subprocess.run(  # noqa: S603
        [sys.executable, "-I", str(Path(__file__).with_name("_deferred_bus_child.py"))],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip().endswith("[]")

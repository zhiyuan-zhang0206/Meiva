# meiva:note
# The steps `meiva/boot.py` takes after its plans: the installed-state steps, the missing-name
# answer, and the two external-process boots. The plans themselves are tables in
# `meiva/sdk_surface/tests/test_identity_plan.py`; these tests drive the steps over a real slot value.
from __future__ import annotations

import json
import random
from pathlib import Path

import pytest

from base.agents.context import MeivaContext
from base.agents.context.clients import ClientSet
from base.agents.sdk.tests.sampling_owners import fixed_sampling_owner
from base.clock import SYSTEM_TIMERS, Clock, ClockConfig
from base.cluster.machine import MachineIdentityReader
from base.config.tests.configuration import snapshot_process_config
from base.events.live.bus import EventBus
from base.paths.tests.homes import unit_home as unit_home
from meiva import boot
from meiva.sdk_surface import install, process_context
from meiva.sdk_surface.installation import Installation, Loading, installed, slot
from meiva.sdk_surface.pure.load_plan import LoadStep
from meiva.sdk_surface.tests.installation_holder import held_installation
from meiva.self.harness.attachment import attachment_clock
from meiva.tests.bootstrap.probe_runner import run_probe

_CLOCK = Clock(ClockConfig("UTC", "UTC", False), timers=SYSTEM_TIMERS)


# meiva:note
# The state gate of a load that never runs agent code: it admits what plugins declare as it is.
def _accept_state(declared: object) -> object:
    return declared


@pytest.mark.parametrize("step", [LoadStep.BIND_CATALOG, LoadStep.NOTHING])
def test_steps_that_change_nothing_leave_the_slot_as_it_was(step: LoadStep) -> None:
    held = Installation.bare(sampling=fixed_sampling_owner())
    with held_installation(held):
        boot.settle_installed(step, None)
        assert slot() is held


def test_the_recorded_failure_step_raises_the_recorded_failure() -> None:
    failure = RuntimeError("plugin load failed earlier")
    with held_installation(Loading(failure=failure)), pytest.raises(RuntimeError) as caught:
        boot.settle_installed(LoadStep.RAISE_FAILURE, None)
    assert caught.value is failure


def test_a_surface_load_is_not_a_step_that_settles_an_installed_state() -> None:
    with pytest.raises(ValueError, match="needs its owners"):
        boot.settle_installed(LoadStep.LOAD_SURFACE, None)


def test_a_private_name_never_loads_plugins() -> None:
    assert boot.maybe_load_plugins_for_missing("_private") is False


def test_a_missing_name_after_a_recorded_failure_raises_it() -> None:
    failure = RuntimeError("plugin load failed earlier")
    with held_installation(Loading(failure=failure)), pytest.raises(RuntimeError) as caught:
        boot.maybe_load_plugins_for_missing("tasks")
    assert caught.value is failure


def test_a_missing_name_on_an_installed_process_loads_nothing() -> None:
    with held_installation(Installation.bare(sampling=fixed_sampling_owner())):
        assert boot.maybe_load_plugins_for_missing("tasks") is False


def test_an_owned_boot_carries_the_roots_of_its_configuration() -> None:
    config = snapshot_process_config()
    owned = boot.own_external_process(config)
    try:
        assert isinstance(owned, boot.OwnedBoot)
        assert owned.config is config
        assert owned.new_context().clients is owned.clients
        assert owned.new_context().require_machine() is owned.machine
        assert owned.new_context().require_clock() is owned.clock
    finally:
        owned.clients.close()


# meiva:note
# An attachment draws its identifiers from the operating system's generator and carries the two
# homes its boot read once, so nothing it runs reads them again.
def test_an_attachment_context_carries_the_homes_and_the_system_generator_of_its_boot() -> None:
    owned = boot.own_external_process(snapshot_process_config())
    try:
        context = owned.new_context()
        assert context.require_user_home() == Path.home()
        assert context.require_meiva_home() == owned.meiva_home
        assert isinstance(context.require_rng(), random.SystemRandom)
    finally:
        owned.clients.close()


# meiva:note
# An attachment's bus is built when its context first needs one, over the boot's configuration, and
# every context of the boot answers that one bus.
def test_an_attachment_context_builds_the_boots_one_event_bus_on_first_read() -> None:
    owned = boot.own_external_process(snapshot_process_config())
    try:
        context = owned.new_context()
        assert context.bus is None
        bus = context.require_bus()
        assert isinstance(bus, EventBus)
        assert owned.new_context().require_bus() is bus
    finally:
        owned.clients.close()


# meiva:note
# A launched script's context carries the same values, with a plain generator (nothing it draws
# is a secret), and the SDK's sampling owner is built over that same generator, so the process has
# one.
def test_a_launched_context_carries_the_homes_and_the_generator_its_sampling_owner_uses(
    unit_home: Path,
) -> None:
    config = snapshot_process_config()
    context = boot.launched_context(env={"MEIVA_AGENT_ID": "7"}, config=config)
    assert context is not None
    assert context.require_user_home() == Path.home()
    assert context.require_meiva_home() == unit_home
    assert not isinstance(context.require_rng(), random.SystemRandom)
    assert context.bus is None
    assert isinstance(context.require_bus(), EventBus)

    previous = process_context.unbind_context()
    try:
        boot.ensure_plugins_loaded(
            surface=False,
            config=config,
            context=context,
            clients=context.clients,
            validate_state=_accept_state,
        )
        installation = installed()
        assert installation is not None
        assert installation.sampling.rng is context.require_rng()
    finally:
        install.uninstall()
        process_context.unbind_context()
        if previous is not None:
            process_context.bind_context(previous)


def test_a_borrowed_boot_takes_the_machine_and_database_of_the_bound_context(
    machine_identity: MachineIdentityReader,
) -> None:
    bound = MeivaContext(machine=machine_identity, clients=ClientSet())
    borrowed = boot.borrow_external_process(bound)
    assert isinstance(borrowed, boot.BorrowedBoot)
    assert borrowed.machine is machine_identity
    assert borrowed.database == bound.clients.database


def test_an_attachment_is_dated_by_the_clock_of_what_it_boots_from(
    machine_identity: MachineIdentityReader,
) -> None:
    config = snapshot_process_config()
    owned = boot.own_external_process(config)
    try:
        assert attachment_clock(None, owned) is owned.clock
    finally:
        owned.clients.close()

    bound = MeivaContext(
        machine=machine_identity, clients=ClientSet(), clock_factory=lambda: _CLOCK
    )
    assert attachment_clock(bound, boot.borrow_external_process(bound)) is _CLOCK


def test_a_borrowed_context_without_a_clock_fails_before_an_attachment_exists(
    machine_identity: MachineIdentityReader,
) -> None:
    bound = MeivaContext(machine=machine_identity, clients=ClientSet())
    borrowed = boot.borrow_external_process(bound)
    with pytest.raises(RuntimeError, match="no clock factory"):
        attachment_clock(bound, borrowed)
    with pytest.raises(RuntimeError, match="needs the context it borrows"):
        attachment_clock(None, borrowed)


# meiva:note
# `import meiva` must stay off the Redis, Postgres and full-configuration stacks, and a launched
# script's context is built there, so building it with a lite configuration loads none of them: the
# bus waits for the first read. Only a fresh interpreter shows what a build pulled in.
def test_building_a_launched_context_loads_no_bus_redis_or_full_configuration(
    tmp_path: Path,
) -> None:
    proc = run_probe(
        "launched_context_stays_cold",
        env={"MEIVA_HOME": str(tmp_path), "MEIVA_PROCESS_PROFILE": "agent"},
    )

    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout.strip().splitlines()[-1]) == {
        "eager_bus": False,
        "bus_factory": True,
        "loaded": [],
    }

# meiva:note
# The steps `meiva/boot.py` takes after its plans: the installed-state steps, the missing-name
# answer, and the two external-process boots. The plans themselves are tables in
# `meiva/sdk_surface/tests/test_identity_plan.py`; these tests drive the steps over a real slot value.
from __future__ import annotations

import pytest

from base.agents.context import MeivaContext
from base.agents.context.clients import ClientSet
from base.clock import SYSTEM_TIMERS, Clock, ClockConfig
from base.cluster.machine import MachineIdentity
from base.config.tests.configuration import snapshot_process_config
from meiva import boot
from meiva.sdk_surface.installation import Installation, Loading, slot
from meiva.sdk_surface.pure.load_plan import LoadStep
from meiva.sdk_surface.tests.installation_holder import held_installation, inert_sampling
from meiva.self.harness.attachment import attachment_clock

_CLOCK = Clock(ClockConfig("UTC", "UTC", False), timers=SYSTEM_TIMERS)


@pytest.mark.parametrize("step", [LoadStep.BIND_CATALOG, LoadStep.NOTHING])
def test_steps_that_change_nothing_leave_the_slot_as_it_was(step: LoadStep) -> None:
    held = Installation.bare(sampling=inert_sampling())
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
    with held_installation(Installation.bare(sampling=inert_sampling())):
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


def test_a_borrowed_boot_takes_the_machine_and_database_of_the_bound_context(
    machine_identity: MachineIdentity,
) -> None:
    bound = MeivaContext(machine=machine_identity, clients=ClientSet())
    borrowed = boot.borrow_external_process(bound)
    assert isinstance(borrowed, boot.BorrowedBoot)
    assert borrowed.machine is machine_identity
    assert borrowed.database == bound.clients.database


def test_an_attachment_is_dated_by_the_clock_of_what_it_boots_from(
    machine_identity: MachineIdentity,
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
    machine_identity: MachineIdentity,
) -> None:
    bound = MeivaContext(machine=machine_identity, clients=ClientSet())
    borrowed = boot.borrow_external_process(bound)
    with pytest.raises(RuntimeError, match="no clock factory"):
        attachment_clock(bound, borrowed)
    with pytest.raises(RuntimeError, match="needs the context it borrows"):
        attachment_clock(None, borrowed)

"""Machine SDK reads require a context, without requiring an agent or plugins."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace

import pytest

import meiva.self as self_mod
from base.agents.context import MeivaContext
from base.cluster.machine import MachineFacts, MachineNameMissing, MachineRoleMissing
from base.cluster.machine.configured import ConfiguredMachineIdentity
from base.config import ConfigBoot
from meiva.sdk_surface import install as sdk_install
from meiva.sdk_surface import process_context
from meiva.sdk_surface.installation import installed


@pytest.fixture
def restore_context() -> Iterator[None]:
    prior = process_context.unbind_context()
    if prior is not None:
        process_context.bind_context(prior)
    try:
        yield
    finally:
        process_context.unbind_context()
        if prior is not None:
            process_context.bind_context(prior)


def _machine(name: str, description: str | None) -> MachineFacts:
    return MachineFacts.of(
        name=name,
        role=frozenset({"gateway"}),
        host="localhost",
        description=description,
    )


@pytest.mark.usefixtures("restore_context")
def test_machine_spec_tuple_with_description() -> None:
    process_context.bind_context(MeivaContext(machine=_machine("test-host", "voice IO + browser")))
    spec = self_mod.MACHINE_SPEC
    assert spec == ("test-host", "voice IO + browser")
    assert isinstance(spec, self_mod.MachineSpec)
    assert (spec.name, spec.description) == ("test-host", "voice IO + browser")
    name, description = spec
    assert name == "test-host" and description == "voice IO + browser"


@pytest.mark.usefixtures("restore_context")
def test_machine_spec_tuple_empty_when_no_description() -> None:
    process_context.bind_context(MeivaContext(machine=_machine("runner-a", None)))
    assert self_mod.MACHINE_SPEC == ("runner-a", "")


@pytest.mark.usefixtures("restore_context")
def test_machine_reads_require_a_context() -> None:
    process_context.unbind_context()
    with pytest.raises(AttributeError, match="context"):
        _ = self_mod.MACHINE_SPEC


@pytest.mark.usefixtures("restore_context")
def test_partial_context_has_no_machine_fallback() -> None:
    process_context.bind_context(MeivaContext())
    with pytest.raises(RuntimeError, match="no MachineIdentityReader"):
        _ = self_mod.MACHINE_SPEC


@pytest.mark.usefixtures("restore_context")
def test_no_agent_machine_owner_stays_lazy_and_independent() -> None:
    installation_before = installed()
    load_attempted_before = sdk_install.load_attempted()

    config = ConfigBoot()
    config.read_process_environment()
    config.set_field("machine_name", "")
    config.set_field("machine_serve_gateway", False)
    config.set_field("machine_serve_agent_runner", False)
    config.set_field("machine_serve_observability_station", False)
    machine = ConfiguredMachineIdentity(config)
    context = MeivaContext(machine=machine)
    process_context.bind_context(context)
    config.set_field("machine_name", "explicit")
    config.set_field("machine_description", "owned")
    assert self_mod.MACHINE_SPEC == ("explicit", "owned")
    config.set_field("machine_name", "")
    config.set_field("machine_description", "mutated")
    assert self_mod.MACHINE_SPEC == ("explicit", "owned")
    with pytest.raises(MachineRoleMissing):
        machine.role()
    assert context.identity is None
    assert context.db is None
    process_context.bind_context(replace(context, machine=_machine("other", None)))
    assert self_mod.MACHINE_SPEC == ("other", "")
    assert machine.name() == "explicit"
    assert installed() is installation_before
    assert sdk_install.load_attempted() is load_attempted_before


@pytest.mark.usefixtures("restore_context")
def test_owned_machine_preserves_missing_name_error() -> None:
    process_context.bind_context(MeivaContext(machine=_machine("", None)))
    with pytest.raises(MachineNameMissing):
        _ = self_mod.MACHINE_SPEC

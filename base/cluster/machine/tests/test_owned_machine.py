import pytest

from base.cluster.machine import MachineNameMissing
from base.cluster.machine.boot import machine_from_boot
from base.config import ConfigBoot


class _RefusingConfigBoot(ConfigBoot):
    # meiva:note
    # A boot config that fails the test if construction reads any field from it.
    def get_field(self, name: str) -> object:
        pytest.fail(f"construction must not read machine configuration (read {name!r})")


def test_construction_does_not_read_configuration() -> None:
    machine_from_boot(_RefusingConfigBoot())


def test_role_never_requires_name_and_delivery_owners_are_independent() -> None:
    prior_config = ConfigBoot()
    prior_config.read_process_environment()
    prior_config.set_field("machine_name", "")
    prior_config.set_field("machine_serve_gateway", True)
    prior_config.set_field("machine_serve_agent_runner", False)
    prior_config.set_field("machine_serve_observability_station", False)
    prior = machine_from_boot(prior_config)
    assert prior.role() == frozenset({"gateway"})
    with pytest.raises(MachineNameMissing):
        prior.name()
    next_config = ConfigBoot()
    next_config.read_process_environment()
    next_config.set_field("machine_name", "next-host")
    current = machine_from_boot(next_config)
    assert current.name() == "next-host"
    prior.reset()
    with pytest.raises(MachineNameMissing):
        prior.name()
    assert current.name() == "next-host"


def test_owner_reset_observes_repaired_fields_without_resetting_another() -> None:
    config = ConfigBoot()
    config.read_process_environment()
    config.view.general.machine_name = "before"
    config.view.general.machine_description = "first"
    machine = machine_from_boot(config)
    other = machine_from_boot(config)
    assert machine.name() == other.name() == "before"
    assert machine.description() == "first"
    config.view.general.machine_name = "after"
    config.view.general.machine_description = "second"
    assert machine.name() == "before"
    machine.reset()
    assert machine.name() == "after"
    assert machine.description() == "second"
    assert other.name() == "before"

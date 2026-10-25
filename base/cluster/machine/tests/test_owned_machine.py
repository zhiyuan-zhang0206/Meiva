from pathlib import Path

import pytest

from base.cluster.machine import MachineNameMissing
from base.cluster.machine.configured import ConfiguredMachineIdentity
from base.config import ConfigBoot
from base.paths.tests.homes import point_home


def test_construction_does_not_read_configuration(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    point_home(monkeypatch, tmp_path)
    (tmp_path / ".env").write_bytes(b"\xff")
    config = ConfigBoot()
    owner = ConfiguredMachineIdentity(config)
    assert not config.prepared
    owner.set(role="gateway")
    assert owner.role() == frozenset({"gateway"})
    assert not config.prepared
    with pytest.raises(UnicodeDecodeError):
        owner.name()


def test_role_never_requires_name_and_delivery_owners_are_independent() -> None:
    prior_config = ConfigBoot()
    prior_config.read_process_environment()
    prior_config.set_field("machine_name", "")
    prior_config.set_field("machine_serve_gateway", True)
    prior_config.set_field("machine_serve_agent_runner", False)
    prior_config.set_field("machine_serve_observability_station", False)
    prior = ConfiguredMachineIdentity(prior_config)
    assert prior.role() == frozenset({"gateway"})
    with pytest.raises(MachineNameMissing):
        prior.name()
    next_config = ConfigBoot()
    next_config.read_process_environment()
    next_config.set_field("machine_name", "next-host")
    current = ConfiguredMachineIdentity(next_config)
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
    machine = ConfiguredMachineIdentity(config)
    other = ConfiguredMachineIdentity(config)
    assert machine.name() == other.name() == "before"
    assert machine.description() == "first"
    config.view.general.machine_name = "after"
    config.view.general.machine_description = "second"
    assert machine.name() == "before"
    machine.reset()
    assert machine.name() == "after"
    assert machine.description() == "second"
    assert other.name() == "before"

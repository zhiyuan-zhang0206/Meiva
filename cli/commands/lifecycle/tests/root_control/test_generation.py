# meiva:note
# The write generation a launch digest binds: the ledger's, or an installed capability's.

from __future__ import annotations

from pathlib import Path

from base.cluster.authority.tests.unit_capability_support import gateway as gateway
from base.cluster.authority.tests.unit_capability_support import runner_boot as runner_boot
from base.cluster.authority.tests.unit_capability_support import runner_home as runner_home
from base.host.db_authority import unit
from cli.commands.lifecycle.root_control.generation import write_generation


def test_a_pure_runners_generation_is_its_installed_capabilitys_reference(
    runner_boot: unit.UnitCapability, runner_home: Path
) -> None:
    assert write_generation(runner_home) == runner_boot.reference


def test_a_pure_runner_whose_capability_is_gone_binds_no_generation(
    runner_boot: unit.UnitCapability, runner_home: Path
) -> None:
    unit.unit_capability_path(runner_home).unlink()

    assert write_generation(runner_home) is None

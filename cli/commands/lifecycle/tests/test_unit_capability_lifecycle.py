from __future__ import annotations

from pathlib import Path

import pytest

from base.cluster.authority.tests.unit_capability_support import gateway as gateway
from base.cluster.authority.tests.unit_capability_support import runner_boot as runner_boot
from base.cluster.authority.tests.unit_capability_support import runner_home as runner_home
from base.host.db_authority import delivery, unit
from cli.commands.data_plane import bringup
from cli.commands.data_plane.instance.launch_delivery import LaunchDelivery


def test_a_pure_runners_launcher_delivers_the_installed_capability(
    runner_boot: unit.UnitCapability, runner_home: Path
) -> None:
    launch = LaunchDelivery(home=runner_home, remote_data_plane=False, environment={}, env_file={})
    assert bringup.db_delivery("runner", delivery=launch) == {
        "MEIVA_DB_URL": runner_boot.dsn,
        delivery.GENERATION_ENV: "0",
    }
    with pytest.raises(RuntimeError, match="cannot launch a gateway-class"):
        bringup.db_delivery("gateway", delivery=launch)
    unit.unit_capability_path(runner_home).unlink()
    with pytest.raises(unit.UnitCapabilityError, match="holds no database capability"):
        bringup.db_delivery("runner", delivery=launch)

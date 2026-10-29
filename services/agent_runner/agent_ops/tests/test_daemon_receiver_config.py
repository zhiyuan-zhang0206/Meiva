# meiva:note
# The upload receiver takes the gateway access its entry already built. One start of the daemon
# answers the home's gateway question once and hands the same object to the receiver and to the
# bearer set `/ops` admits; a receiver that resolved it itself would read that answer twice.

from __future__ import annotations

from pathlib import Path
from typing import cast

from base.cluster.machine import GatewayAccess
from services.agent_runner.agent_ops.daemon import receiver_config


def test_the_receiver_takes_the_access_the_entry_built(tmp_path: Path) -> None:
    # meiva:note
    # Any object will do: the assertion is identity, so a receiver that built its own access could
    # not pass it.
    access = cast(GatewayAccess, object())

    built = receiver_config(machine="ops-entry", home=tmp_path, access=access)

    assert built.access is access
    assert built.unit.machine == "ops-entry"

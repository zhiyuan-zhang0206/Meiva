# meiva:note
# The agent host's side of the timing topology: the clocks its beat runs on, and the one place it
# judges the values it will live by — the boot guard, which runs before the instance lock and the
# pools, so a configuration that would outlive a declared bound stops the process instead of a wake.
# meiva:link docs/decisions/runtime/config/2026-10-28-held-settle-budget-configurable.md

from __future__ import annotations

from pathlib import Path

import pytest

import base.deploy.progress_timeout as deploy
from base.cluster.machine.configured import ConfiguredMachineIdentity
from base.config import ConfigBoot
from base.db import Database
from base.deploy.timing import CLOCKS, ClockLatticeError
from services.agent_runner.agent_host.daemon import run


def test_beat_clock_is_the_agent_hosts_actual_beat() -> None:
    from services.agent_runner.agent_host import host_beat

    assert CLOCKS["AGENT_HOST_BEAT_INTERVAL_S"].get() == host_beat.LIVENESS_BEAT_STEP_S


async def test_a_settle_budget_at_the_stall_bound_stops_the_boot_before_the_lock(
    tmp_path: Path,
) -> None:
    boot = ConfigBoot()
    boot.set_field("held_settle_budget_s", deploy.NO_PROGRESS_TIMEOUT_S)

    def never_opened() -> Database:
        raise AssertionError("the settle budget is judged before the database is opened")

    with pytest.raises(ClockLatticeError, match="HANDOFF_SETTLE_RETRY_BUDGET_S"):
        await run(
            config=boot,
            database=never_opened,
            machine=ConfiguredMachineIdentity(boot),
            home=tmp_path,
            user_home=tmp_path,
            disabled_entries=(),
        )
    assert list(tmp_path.iterdir()) == [], "the refusal lands before the boot touches its home"

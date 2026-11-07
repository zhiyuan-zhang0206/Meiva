# meiva:note
# The agent host's side of the timing topology: the clocks its beat runs on, and the one place it
# judges the values it will live by — the boot guard, which runs before the instance lock and the
# pools, so a configuration that would outlive a declared bound stops the process instead of a wake.
# meiva:link docs/decisions/runtime/config/2026-10-28-held-settle-budget-configurable.md

from __future__ import annotations

from base.deploy.timing import CLOCKS


def test_beat_clock_is_the_agent_hosts_actual_beat() -> None:
    from services.agent_runner.agent_host import host_beat

    assert CLOCKS["AGENT_HOST_BEAT_INTERVAL_S"].get() == host_beat.LIVENESS_BEAT_STEP_S

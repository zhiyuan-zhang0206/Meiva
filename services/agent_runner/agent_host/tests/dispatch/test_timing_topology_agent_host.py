from __future__ import annotations

from base.deploy.timing import CLOCKS


def test_beat_clock_is_the_agent_hosts_actual_beat() -> None:
    from services.agent_runner.agent_host import daemon as agent_host_daemon

    assert CLOCKS["AGENT_HOST_BEAT_INTERVAL_S"].get() == agent_host_daemon.LIVENESS_BEAT_STEP_S

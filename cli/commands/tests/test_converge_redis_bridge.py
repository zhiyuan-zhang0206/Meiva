# meiva:note
# Converge retires the standalone Redis bridge on gateway hosts. The relay's own listener
# rebuilding and back-off are tested against real sockets in `services/redis_bridge/tests`.

from __future__ import annotations

from cli.commands.converge import redis_bridge as bridge
from cli.commands.lifecycle import converge_host


def test_retire_step_runs_on_gateway_hosts() -> None:
    step = next(
        candidate
        for candidate in converge_host.CONVERGE_STEPS
        if candidate.name == "retire the standalone Redis bridge LaunchAgent"
    )

    assert step.apply is bridge.retire_launchd_redis_bridge
    assert step.roles == frozenset({"gateway"})
    assert step.host_global

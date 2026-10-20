"""Loopback health routes expose the actual hosted owner and cancellation boundary."""

import os

from base import paths
from base.daemon.health import RouteHandler
from services.agent_runner.agent_host.dispatcher import TurnScheduler
from services.agent_runner.agent_host.host import AgentHost


# meiva:note
# A `POST /cancel-turn` handler — the stop button, force terminate and wedged
#     recovery primitive.
#
#     Body: `{"agent_id": <int>}`. Cancels the agent's running turn task with the
#     bounded unwind (a C-call-blocked turn is reported, not awaited forever) and
#     answers `{"cancelled": true|false}` — false means no turn was running here.
#     The route changes no row: whoever cancels owns the agent's status.
#
#     Loopback-only and unauthenticated, like `/healthz`: anything that can dial
#     the host's localhost health port already owns the box.
def cancel_turn_route(scheduler: TurnScheduler) -> RouteHandler:

    async def handler(body: bytes) -> tuple[int, bytes, str]:
        import json

        try:
            agent_id = json.loads(body or b"{}")["agent_id"]
        except (ValueError, KeyError, TypeError, json.JSONDecodeError):
            return 400, b'{"error":"positive agent_id required"}', "application/json"
        if type(agent_id) is not int or agent_id <= 0:
            return 400, b'{"error":"positive agent_id required"}', "application/json"
        cancelled = await scheduler.cancel_turn(agent_id)
        return 200, json.dumps({"cancelled": cancelled}).encode(), "application/json"

    return handler


def stats_route(host: AgentHost, scheduler: TurnScheduler) -> RouteHandler:
    """Expose cache/activity counters and this running boot's maintenance identity."""
    import json

    async def handler(_body: bytes) -> tuple[int, bytes, str]:
        # Per-agent turn-progress age: the health signal that separates
        # "the host process is alive" from "this turn is alive". A busy agent
        # (progress every couple of minutes) reads small; an agent whose
        # invocation has been silent for the wedged budget reads large — the
        # turn-level fake-alive state a heartbeat probe alone cannot see.
        active_progress: dict[int, float] = {}
        for agent_id in sorted(scheduler.active_agents):
            age = host.turn_progress.age_s(agent_id)
            if age is not None:
                active_progress[agent_id] = round(age, 1)
        payload = {
            **host.stats.as_payload(),
            **host.admission.payload(),
            "maintenance_protocol": 1,
            "runtime_owner": str(host.runtime_owner),
            "home": str(paths.meiva_home()),
            "pid": os.getpid(),
            "active_agents": sorted(scheduler.active_agents),
            "active_progress": active_progress,
        }
        return 200, json.dumps(payload).encode(), "application/json"

    return handler

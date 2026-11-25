"""Loopback health routes expose the actual hosted owner and cancellation boundary."""

from pathlib import Path

from base.daemon.health import RouteHandler
from base.host.boundary.json_values import is_object_dict
from base.host.boundary.untyped import object_of
from services.agent_runner.agent_host.dispatcher import TurnScheduler
from services.agent_runner.agent_host.host import AgentHost


def _json_object(body: bytes) -> dict[str, object] | None:
    import json

    try:
        value = object_of(json.loads, body or b"{}")
    except ValueError:
        return None
    return value if is_object_dict(value) else None


# meiva:note
# A `POST /cancel-turn` handler — the interrupt of a native turn, force terminate and wedged
#     recovery primitive.
#
#     Body: `{"agent_id": <int>, "rearm": <bool, optional>}`; `rearm` starts the next turn at once
#     when a wake arrived during the cancelled one (an interrupt's message). Cancels the agent's running turn task with the
#     bounded unwind (a C-call-blocked turn is reported, not awaited forever) and
#     answers `{"cancelled": true|false}` — false means no turn was running here.
#     The route changes no row: whoever cancels owns the agent's status.
#
#     Loopback-only and unauthenticated, like `/healthz`: anything that can dial
#     the host's localhost health port already owns the box.
def cancel_turn_route(scheduler: TurnScheduler) -> RouteHandler:

    async def handler(body: bytes) -> tuple[int, bytes, str]:
        import json

        request = _json_object(body)
        agent_id = None if request is None else request.get("agent_id")
        if request is None or type(agent_id) is not int or agent_id <= 0:
            return 400, b'{"error":"positive agent_id required"}', "application/json"
        cancelled = await scheduler.cancel_turn(agent_id, rearm=request.get("rearm") is True)
        return 200, json.dumps({"cancelled": cancelled}).encode(), "application/json"

    return handler


# meiva:note
# A `POST /prepare-harness-switch` handler: the agent's cached runtime is dropped before its harness
# changes, so the next turn builds on the stored harness. The switch itself is shown by the epoch
# table; nothing is written to the outgoing history.
def prepare_harness_switch_route(host: AgentHost) -> RouteHandler:

    async def handler(body: bytes) -> tuple[int, bytes, str]:
        payload = _json_object(body)
        agent_id = None if payload is None else payload.get("agent_id")
        if type(agent_id) is not int or agent_id <= 0:
            return 400, b'{"error":"positive agent_id required"}', "application/json"
        host.drop_agent(agent_id)
        return 200, b'{"ok":true}', "application/json"

    return handler


def stats_route(host: AgentHost, scheduler: TurnScheduler, *, pid: int, home: Path) -> RouteHandler:
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
            "kept_alive_sessions": host.kept_alive_sessions(),
            **host.admission.payload(),
            "maintenance_protocol": 1,
            "runtime_owner": str(host.runtime_owner),
            "home": str(home),
            "pid": pid,
            "active_agents": sorted(scheduler.active_agents),
            "active_progress": active_progress,
        }
        return 200, json.dumps(payload).encode(), "application/json"

    return handler

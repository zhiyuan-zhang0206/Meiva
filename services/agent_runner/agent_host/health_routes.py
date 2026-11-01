"""Loopback health routes expose the actual hosted owner and cancellation boundary."""

from pathlib import Path

from base.clock import Clock
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


# meiva:note
# A `POST /prepare-harness-switch` handler — records harness switch before termination and drops sessions.
def prepare_harness_switch_route(host: AgentHost, *, clock: Clock) -> RouteHandler:

    async def handler(body: bytes) -> tuple[int, bytes, str]:
        import json

        from agent.messages import NoteTag, system_note_message
        from base.agents.messages.lifecycle_text import by_who

        try:
            payload = json.loads(body or b"{}")
            agent_id = payload["agent_id"]
            old_harness = payload["old_harness"]
            new_harness = payload["new_harness"]
            source = payload["source"]
            message = payload["message"]
        except (ValueError, KeyError, TypeError, json.JSONDecodeError):
            return 400, b'{"error":"invalid prepare-harness-switch payload"}', "application/json"
        if type(agent_id) is not int or agent_id <= 0:
            return 400, b'{"error":"positive agent_id required"}', "application/json"

        if old_harness != new_harness:
            from agent.graph.node_log import SNAPSHOT_CURSOR

            SNAPSHOT_CURSOR.pop(agent_id, None)

            now = clock.now()
            ts_prefix = f"[{now.strftime('%Y-%m-%d %H:%M:%S UTC')}] "
            note_content = f"{ts_prefix}Harness switched from '{old_harness}' to '{new_harness}' by {by_who(source)}: {message}"
            if old_harness == "meiva":
                note = system_note_message(
                    content=note_content,
                    tag=NoteTag.LIFECYCLE_RESTART,
                    picked_up_at=now,
                    created_at=now,
                )
                try:
                    await host._graph.aupdate_state(
                        {"configurable": {"thread_id": str(agent_id)}},
                        {"messages": [note]},
                    )
                except Exception as exc:
                    import logging

                    logging.getLogger(__name__).warning(
                        "[health] failed to record harness switch note in meiva state for agent %s: %s",
                        agent_id,
                        exc,
                    )
            else:
                from base.agents.history.harness_messages import aappend_harness_timeline_items
                from base.agents.history.timeline_item import TimelineItem
                from base.db.transaction import async_write_transaction

                note_item = TimelineItem(
                    item_id="0.0",
                    kind="system_marker",
                    source="lifecycle_restart",
                    payload=note_content,
                    created_at=now.isoformat(),
                )
                pool = getattr(host, "_control_pool", getattr(host, "_pool", None))
                if pool is not None:
                    try:
                        async with async_write_transaction(pool) as conn:
                            await aappend_harness_timeline_items(
                                conn, agent_id, old_harness, [note_item]
                            )
                    except Exception as exc:
                        import logging

                        logging.getLogger(__name__).warning(
                            "[health] failed to record harness switch note in harness_messages for agent %s: %s",
                            agent_id,
                            exc,
                        )

        host.drop_agent(agent_id)
        registry = getattr(host, "_harness_registry", None)
        if registry is not None:
            registry.delete(agent_id)

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

"""Best-effort live hints published after durable writes commit.

Lifecycle hints name the changed agent without reading or embedding its state.
Consumers reconcile authoritative views, so an older announcement cannot replace
newer state. No database connection is needed on the announcement path.

Redis live hints are separate from durable telemetry facts. Publication failure
must not roll back or fail the already-committed lifecycle write.
"""

from __future__ import annotations

from base.events.live.bus import EventBus
from base.events.live.projection import (
    AgentSpawned,
    AgentUpdated,
)


def publish_agent_spawned_sync(bus: EventBus, agent_id: int) -> None:
    """Publish a committed creation hint without reading agent state."""
    ev = AgentSpawned(agent_id=agent_id)
    bus.publish_best_effort_sync(ev.model_dump_json(), context="agent_spawned")


def publish_agent_updated_sync(bus: EventBus, agent_id: int) -> None:
    """Publish a committed lifecycle/display-state hint without a database read."""
    ev = AgentUpdated(agent_id=agent_id)
    bus.publish_best_effort_sync(ev.model_dump_json(), context="agent_updated")


async def publish_agent_updated(bus: EventBus, agent_id: int) -> None:
    """Publish a committed change hint from an async context."""
    ev = AgentUpdated(agent_id=agent_id)
    await bus.publish_best_effort(ev.model_dump_json(), context="agent_updated")

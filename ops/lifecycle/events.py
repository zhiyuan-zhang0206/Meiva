# meiva:note
# Event-publish RPC ops — the InboundArrived / Notice event fan-out.
#
# The `ops.lifecycle` door re-exports these publishes. Each helper is best-effort: the publish goes to
# the shared events channel (`EventBus.publish_best_effort`) so a Redis blip costs a live
# UI update, never the durable op that triggered it.

from __future__ import annotations

from base.events.live.bus import EventBus
from base.events.live.projection import (
    InboundArrived,
    QuestionAsked,
    QuestionResolved,
)


async def publish_inbound_arrived(
    bus: EventBus, agent_id: int, inbound_id: int, kind: str, source: str, content: str
) -> None:
    """Publish InboundArrived to the events channel — frontend live updates."""
    ev = InboundArrived(
        agent_id=agent_id,
        inbound_id=inbound_id,
        kind=kind,
        source=source,
        content=content,
    )
    await bus.publish_best_effort(ev.model_dump_json(), context="inbound_arrived")


# meiva:note
# Publish QuestionResolved so the frontend refetches the open questions. Best-effort, never raises.
async def publish_question_resolved(bus: EventBus, agent_id: int, question_id: int) -> None:
    ev = QuestionResolved(agent_id=agent_id, question_id=question_id)
    await bus.publish_best_effort(ev.model_dump_json(), context="question_resolved")


# meiva:note
# Publish QuestionAsked so the frontend refetches the open questions. Best-effort, never raises.
# This is the refetch hint only. The delivery decision for a question is staged in its own
# transaction and announced by the producer after it commits, so the hint and the user-facing
# event cannot disagree.
# meiva:link base/events/notifications/README.md
async def publish_question_asked(
    bus: EventBus, agent_id: int, question_id: int, title: str
) -> None:
    ev = QuestionAsked(agent_id=agent_id, question_id=question_id, title=title)
    await bus.publish_best_effort(ev.model_dump_json(), context="question_asked")

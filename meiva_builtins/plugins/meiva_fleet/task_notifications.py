from __future__ import annotations

import logging

_log = logging.getLogger("meiva_fleet.task_notifications")


# meiva:note
# Delivery follows the `meiva_fleet.task_closed` category policy, so a retried
# update or a replayed operation key cannot notify twice, and a delivery failure
# never fails the update — the task row is already committed.
# meiva:link base/events/notifications/docs/notifications.meiva.okf.md
def deliver_task_closed(agent_id: int, task_id: int, title: str) -> None:
    import meiva
    from base.agents.context.gateway_client import transport

    try:
        resp = transport.post(
            f"/api/agents/{agent_id}/notifications",
            {
                "category": "meiva_fleet.task_closed",
                "subject": f"Task #{task_id} closed: {title}",
                "dedupe_key": f"task:{task_id}:closed",
            },
            context=meiva.context,
        )
        resp.raise_for_status()
    except Exception:
        _log.exception("task-closed notification failed for task %s", task_id)

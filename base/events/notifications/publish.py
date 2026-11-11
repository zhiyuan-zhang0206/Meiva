from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

import psycopg
from psycopg_pool import ConnectionPool

from base.events.live.projection import NotificationEmitted
from base.events.notifications.catalog import category
from base.events.notifications.policy import enabled_for
from base.events.notifications.store import (
    agent_label,
    insert,
    mark_announced_sync,
    read_overrides,
    unannounced_sync,
)
from base.events.notifications.triggers import (
    NotificationAction,
    PushPlan,
    plan_question_asked,
    plan_question_resolved,
    plan_unread_delivery,
    plan_unread_seen,
)

_log = logging.getLogger("base.events.notifications")


@dataclass(frozen=True)
class NotificationRecord:
    id: int
    category: str
    action: str
    tag: str


# meiva:note
# The producing transaction stages its own push: the question row or the delivery row commits with
# its `notification_events` row, so the debt exists before any process could die and a rollback of
# the business write leaves no push behind. `announced_at` stays NULL until the transport accepts
# the publication; the gateway outbox sweep converges it. An alert or a replace follows the user's
# category switch; a retract is always staged, because switching a category off must not strand a
# notification that is already on someone's screen.
# meiva:link base/events/notifications/README.md
# meiva:note
# A tap target is the agent's own page: both kinds of push are about one agent's conversation, and
# the console opens an agent through the `agent_id` query on its root route.
# meiva:link base/events/notifications/README.md
def _agent_href(agent_id: int) -> str:
    return f"/?agent_id={agent_id}"


def stage_in_transaction(
    conn: psycopg.Connection, plan: PushPlan, *, agent_id: int
) -> NotificationRecord | None:
    declared = category(plan.category)
    if not plan.subject.strip():
        raise ValueError(f"notification {declared.id} needs a subject")
    if plan.action is not NotificationAction.RETRACT and not enabled_for(
        declared, read_overrides(conn)
    ):
        _log.debug("notification %s suppressed: category switched off", declared.id)
        return None
    row_id = insert(
        conn,
        (
            declared.id,
            plan.action.value,
            plan.tag,
            plan.subject,
            plan.body,
            _agent_href(agent_id),
            declared.importance,
            agent_id,
        ),
    )
    return NotificationRecord(
        id=row_id, category=declared.id, action=plan.action.value, tag=plan.tag
    )


def stage_unread_delivery(
    conn: psycopg.Connection, agent_id: int, content: str, *, had_unread: bool
) -> None:
    plan = plan_unread_delivery(
        agent_label=agent_label(conn, agent_id),
        content=content,
        agent_id=agent_id,
        had_unread=had_unread,
    )
    stage_in_transaction(conn, plan, agent_id=agent_id)


def stage_unread_seen(
    conn: psycopg.Connection, agent_id: int, *, had_unread: bool, has_unread: bool
) -> None:
    plan = plan_unread_seen(
        agent_label=agent_label(conn, agent_id),
        agent_id=agent_id,
        had_unread=had_unread,
        has_unread=has_unread,
    )
    if plan is not None:
        stage_in_transaction(conn, plan, agent_id=agent_id)


def stage_question_asked(
    conn: psycopg.Connection, *, agent_id: int, question_id: int, title: str
) -> None:
    stage_in_transaction(
        conn,
        plan_question_asked(
            question_id=question_id, title=title, agent_label=agent_label(conn, agent_id)
        ),
        agent_id=agent_id,
    )


def stage_question_resolved(
    conn: psycopg.Connection, *, agent_id: int, question_id: int, title: str
) -> None:
    stage_in_transaction(
        conn,
        plan_question_resolved(
            question_id=question_id, title=title, agent_label=agent_label(conn, agent_id)
        ),
        agent_id=agent_id,
    )


class SyncPublisher(Protocol):
    def publish_best_effort_sync(
        self,
        payload: str,
        *,
        channel: str | None = None,
        decode_responses: bool = True,
        context: str = "",
    ) -> int | None: ...


# meiva:note
# Announcing is one step with two outcomes: the transport accepted the live event
# and the row is marked `announced_at`, or the publish failed (the transport
# returns None for a known Redis/network failure) and the row stays unannounced
# for the outbox sweep. A crash between publish and mark re-announces — an
# at-least-once push the client already deduplicates on `notification_id` — and is
# never a lost one.
# meiva:link base/events/notifications/README.md
def announce_sync(pool: ConnectionPool, bus: SyncPublisher | None, notification_id: int) -> bool:
    if bus is None:
        return False
    row = unannounced_sync(pool, notification_id)
    if row is None:
        return False
    published = bus.publish_best_effort_sync(
        NotificationEmitted.model_validate(
            {
                "agent_id": row.agent_id,
                "notification_id": row.id,
                "category": row.category,
                "action": row.action,
                "tag": row.tag,
                "subject": row.subject,
                "body": row.body,
                "href": row.href,
                "importance": row.importance,
            }
        ).model_dump_json(),
        context="notification_emitted",
    )
    if published is None:
        return False
    return mark_announced_sync(pool, notification_id)

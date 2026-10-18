from __future__ import annotations

import asyncio
import hashlib
import json
import logging

from psycopg_pool import ConnectionPool

from base.events.live.bus import EventBus
from base.events.notifications.publish import (
    NotificationRecord,
    announce_sync,
    deliver_sync,
)

_log = logging.getLogger("base.events.notifications.notices")

_SELECT_NOTICE = (
    "SELECT require_response, title, content, question FROM agent_notices WHERE id = %s"
)


def _question_fingerprint(question: object) -> str:
    payload = json.dumps(question, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()[:12]


def _deliver(pool: ConnectionPool, agent_id: int, notice_id: int) -> NotificationRecord | None:
    with pool.connection() as conn:
        row = conn.execute(_SELECT_NOTICE, (notice_id,)).fetchone()
    if row is None:
        return None
    require_response, title, content, question = row
    if not require_response:
        return None
    return deliver_sync(
        pool,
        category_id="core.needs_input",
        subject=title,
        body=content,
        dedupe_key=f"notice:{notice_id}:{_question_fingerprint(question)}",
        agent_id=agent_id,
    )


# meiva:note
# The notice row is read here rather than passed by the caller: the two post paths
# commit different payload objects, and the spine is the one owner of "which
# category a notice becomes". A notice that needs no answer is not a
# notification — the console inbox already carries it, and a ping the user cannot
# act on is noise. The dedupe key carries the question fingerprint rather than the
# notice id alone, so editing a container to add a question is a new delivery
# while a retried post of the same content stays silent. A delivery failure never
# fails the notice — the row is already committed by the time this runs. The sync
# entry exists for producers outside an event loop (the task-maintenance daemon);
# the async one delegates to it, so there is one decision and one live event
# rather than a weaker second path.
# meiva:link base/events/notifications/docs/notifications.meiva.okf.md
def deliver_notice_notification_sync(
    pool: ConnectionPool, bus: EventBus | None, *, agent_id: int, notice_id: int
) -> int | None:
    try:
        record = _deliver(pool, agent_id, notice_id)
    except Exception:
        _log.exception("notice notification failed for notice %s", notice_id)
        return None
    if record is None:
        return None
    announce_sync(bus, record)
    return record.id


async def deliver_notice_notification(
    pool: ConnectionPool, bus: EventBus | None, *, agent_id: int, notice_id: int
) -> int | None:
    return await asyncio.to_thread(
        deliver_notice_notification_sync, pool, bus, agent_id=agent_id, notice_id=notice_id
    )

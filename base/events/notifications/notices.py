from __future__ import annotations

import asyncio
import logging

from psycopg_pool import ConnectionPool

from base.events.live.bus import EventBus
from base.events.notifications.publish import NotificationRecord, announce, deliver_sync

_log = logging.getLogger("base.events.notifications.notices")

_SELECT_NOTICE = "SELECT require_response, title, content FROM agent_notices WHERE id = %s"


def _deliver(pool: ConnectionPool, agent_id: int, notice_id: int) -> NotificationRecord | None:
    with pool.connection() as conn:
        row = conn.execute(_SELECT_NOTICE, (notice_id,)).fetchone()
    if row is None:
        return None
    require_response, title, content = row
    if not require_response:
        return None
    return deliver_sync(
        pool,
        category_id="core.needs_input",
        subject=title,
        body=content,
        dedupe_key=f"notice:{notice_id}",
        agent_id=agent_id,
    )


# meiva:note
# The notice row is read here rather than passed by the caller: the two post paths
# commit different payload objects, and the spine is the one owner of "which
# category a notice becomes". A notice that needs no answer is not a
# notification — the console inbox already carries it, and a ping the user cannot
# act on is noise. A delivery failure never fails the notice — the row is already
# committed by the time this runs.
# meiva:link base/events/notifications/docs/notifications.meiva.okf.md
async def deliver_notice_notification(
    pool: ConnectionPool, bus: EventBus | None, *, agent_id: int, notice_id: int
) -> int | None:
    try:
        record = await asyncio.to_thread(_deliver, pool, agent_id, notice_id)
    except Exception:
        _log.exception("notice notification failed for notice %s", notice_id)
        return None
    if record is None:
        return None
    await announce(bus, record)
    return record.id

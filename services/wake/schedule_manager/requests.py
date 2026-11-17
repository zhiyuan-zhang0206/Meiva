# meiva:note
# Acknowledge queued schedule updates after the elected manager observes them.
#
# Declarative launches read durable schedule state directly on each reconcile.
# Requests left by earlier gateways can therefore be drained without PTY actions.

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from psycopg_pool import ConnectionPool

from base.db.rows import query_all
from base.db.transaction import write_transaction
from services.wake.schedule_manager.manager import ScheduleManager

_log = logging.getLogger(__name__)

# One consume pass handles at most this many schedules; a larger burst drains
# over the next passes (the loop runs every second).
_BATCH = 50


@dataclass(frozen=True)
class _QueuedRow:
    schedule_id: int
    requested_at: datetime


# meiva:note
# Run the sync of every queued request, oldest first; return how many ran.
#
# The elected manager drains requests; durable schedule state is read by reconcile.
def consume_requests(pool: ConnectionPool, manager: ScheduleManager) -> int:
    if not manager.is_active_unit():
        return 0
    with pool.connection() as conn:
        queued = query_all(
            conn,
            _QueuedRow,
            "SELECT schedule_id, requested_at FROM schedule_sync_requests "
            "WHERE machine = %s "
            "ORDER BY requested_at, schedule_id LIMIT %s",
            (manager.machine_name, _BATCH),
        )
    handled = 0
    for request in queued:
        with write_transaction(pool) as conn:
            conn.execute(
                "DELETE FROM schedule_sync_requests WHERE schedule_id = %s AND machine = %s AND requested_at = %s",
                (request.schedule_id, manager.machine_name, request.requested_at),
            )
        handled += 1
        _log.info("[schedule-manager] synced schedule %s on request", request.schedule_id)
    return handled

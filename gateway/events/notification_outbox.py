from __future__ import annotations

import asyncio
import logging

from psycopg_pool import ConnectionPool

from base.events.live.bus import EventBus
from base.events.notifications.outbox import republish_pending
from gateway.http.middleware.stopping import is_stopping

_log = logging.getLogger("gateway.events.notification_outbox")

# meiva:note
# The resident loop over the outbox: `base.events.notifications.outbox` owns what
# one sweep does, this module owns when the gateway runs it — every
# `notification_outbox_interval_seconds`, and never while the unit is stopping.
# meiva:link base/events/notifications/docs/notifications.meiva.okf.md


# meiva:note
# A failed sweep is a log line, never a crash: every row it did not reach stays
# unannounced, which is exactly the condition that brings the next sweep back to
# it. The loop stops with the gateway's background lifetime.
# meiva:link base/events/notifications/docs/notifications.meiva.okf.md
async def run(pool: ConnectionPool, bus: EventBus, *, interval_seconds: float, batch: int) -> None:
    while True:
        try:
            if not is_stopping():
                announced = await asyncio.to_thread(
                    republish_pending,
                    pool,
                    bus,
                    min_age_seconds=interval_seconds,
                    limit=batch,
                )
                if announced:
                    _log.info("re-announced %d unannounced notification deliveries", announced)
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception("notification outbox sweep failed; pending rows stay pending")
        await asyncio.sleep(interval_seconds)

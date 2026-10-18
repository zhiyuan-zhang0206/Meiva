from __future__ import annotations

import logging

from psycopg_pool import ConnectionPool

from base.events.live.bus import EventBus
from base.events.notifications.publish import announce_sync
from base.events.notifications.store import pending_sync

_log = logging.getLogger("base.events.notifications.outbox")

# meiva:note
# The outbox consumer: `notification_events.announced_at IS NULL` means a producer
# committed a delivery and no process published its live `notification` event,
# because the transport failed the announce or the process died between the
# producing commit and the publish. This module turns those rows back into live
# events.
# meiva:link base/events/notifications/docs/notifications.meiva.okf.md


# meiva:note
# A row younger than `min_age_seconds` is left alone: its producer is still inside
# the request that staged it and will announce it itself, and sweeping it here
# would double-publish every notice. Older than that, no producer is still
# working and the row is genuinely owed. A failed publish inside the sweep leaves
# the row unannounced, which is the condition that brings the next sweep back to
# it — so a Redis outage delays pushes instead of dropping them. Re-announcing a
# row whose mark was lost is at-least-once on purpose: clients deduplicate on
# `notification_id`, and a duplicate ping is cheaper than a missing decision.
# meiva:link base/events/notifications/docs/notifications.meiva.okf.md
def republish_pending(
    pool: ConnectionPool, bus: EventBus | None, *, min_age_seconds: float, limit: int
) -> int:
    if bus is None:
        return 0
    announced = 0
    for notification_id in pending_sync(pool, min_age_seconds=min_age_seconds, limit=limit):
        # meiva:note
        # One row's failure must not decide the rest of the batch. The row stays
        # unannounced, so the next sweep returns to it, while the deliveries
        # behind it go out now instead of waiting for it to clear.
        # meiva:link base/events/notifications/docs/notifications.meiva.okf.md
        try:
            if announce_sync(pool, bus, notification_id):
                announced += 1
        except Exception:
            _log.exception(
                "announce failed for notification %s; the row stays owed", notification_id
            )
    return announced

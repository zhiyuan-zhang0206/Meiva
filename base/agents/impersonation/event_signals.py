# meiva:note
# A log-native lease's log cannot complete on its own in two shapes, both row facts with no
# threshold: an ended lease still waiting on an open source receipt (`seal_stuck`, cleared when
# the source seals or the abandoned-receipt pass reclaims it) and a lease with a failed receipt
# (`capture_failed`, permanent, because a failed receipt never completes its lease).
#
# One event per lease and condition, not one per pass: the emitting pass records the same fact as
# an `event_log_incomplete` entry in that lease's log — the counterpart of the
# `event_delivery_complete` entry that ends it — and a later pass stays quiet while the fact
# holds. The lease's log therefore keeps what could not complete, and the alert rule over the
# event stream owns the notification.
# meiva:link base/agents/impersonation/docs/event-log.meiva.okf.md

from __future__ import annotations

from typing import Any

import psycopg
from psycopg.rows import dict_row

from base.agents.impersonation.history import append
from base.agents.impersonation.status import LOG_PROTOCOL_VERSION, pending_reason
from base.log import logger

CONDITIONS = ("seal_stuck", "capture_failed")

_INCOMPLETE_LEASES = """
WITH leases AS (
    SELECT l.id, l.agent_id, l.session_id, l.machine, l.ended_at,
        l.events_completed_at, l.event_delivery_protocol_version,
        l.event_delivery_pending_reason,
        (l.ended_at IS NOT NULL AND EXISTS (
            SELECT 1 FROM agent_impersonation_event_participants p
            WHERE p.lease_id = l.id AND p.state = 'open')) AS seal_stuck,
        EXISTS (
            SELECT 1 FROM agent_impersonation_event_participants p
            WHERE p.lease_id = l.id AND p.state = 'failed') AS capture_failed,
        ARRAY(
            SELECT p.source_key FROM agent_impersonation_event_participants p
            WHERE p.lease_id = l.id AND p.state IN ('open', 'failed')
            ORDER BY p.opened_at) AS pending_sources,
        ARRAY(
            SELECT e.payload ->> 'condition' FROM agent_impersonation_entries e
            WHERE e.lease_id = l.id AND e.kind = 'lifecycle'
                AND e.payload ->> 'event' = 'event_log_incomplete') AS signalled
    FROM agent_impersonations l
    WHERE l.event_delivery_protocol_version = %s
        AND l.events_completed_at IS NULL
)
SELECT * FROM leases WHERE seal_stuck OR capture_failed
"""


# meiva:note
# Report every lease and condition that has not reported yet, and return how many events that
# emitted. Record then emit inside the caller's transaction: the entry is what keeps the next
# pass quiet, and a rollback withdraws the claim instead of leaving a silent gap. A failure
# between the emit and the commit costs one repeated event, never a dropped one.
def emit_incomplete_event_logs(conn: psycopg.Connection[Any]) -> int:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(_INCOMPLETE_LEASES, (LOG_PROTOCOL_VERSION,))
        leases = cur.fetchall()
    emitted = 0
    for lease in leases:
        reported = set(lease["signalled"])
        for condition in CONDITIONS:
            if not lease[condition] or condition in reported:
                continue
            reason = str(pending_reason(lease) or "unknown")
            append(
                conn,
                str(lease["id"]),
                "lifecycle",
                {
                    "event": "event_log_incomplete",
                    "condition": condition,
                    "pending_reason": reason,
                    "pending_sources": list(lease["pending_sources"]),
                    "lease_ended_at": (
                        None if lease["ended_at"] is None else lease["ended_at"].isoformat()
                    ),
                },
                event_key=f"signal:incomplete:{condition}",
            )
            logger.warning(
                "impersonation event log cannot complete: {condition} (lease {lease_id})",
                event="impersonation_event_log_incomplete",
                agent_id=lease["agent_id"],
                condition=condition,
                lease_id=str(lease["id"]),
                lease_machine=str(lease["machine"]),
                pending_reason=reason,
                pending_sources=list(lease["pending_sources"]),
                session=f"{lease['agent_id']}:{lease['session_id']}",
            )
            emitted += 1
    return emitted

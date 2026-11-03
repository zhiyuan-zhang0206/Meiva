# meiva:note
# A log-native lease's log cannot complete on its own in two shapes, both row facts with no
# threshold: an ended lease still waiting on an open source receipt (`seal_stuck`, cleared when
# the source seals or the abandoned-receipt pass reclaims it) and a lease with a failed receipt
# (`capture_failed`, which never clears because a failed receipt never completes its lease).
#
# Reporting is evidence, not a heartbeat: each lease and condition is reported at most once per
# `repeat_interval` (default a day), and the report is recorded as an `event_log_incomplete` entry
# in that lease's log — the counterpart of the `event_delivery_complete` entry that ends it. A
# condition that never clears therefore keeps re-reporting for as long as it holds, so a permanent
# defect cannot go quiet after its first alert; a condition that clears costs one report. The alert
# rule over the event stream owns the notification.
# meiva:link base/agents/impersonation/docs/event-log.meiva.okf.md

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

import psycopg

from base.agents.impersonation.history import append
from base.agents.impersonation.status import LOG_PROTOCOL_VERSION, pending_reason
from base.db.rows import query_all
from base.host.boundary.json_values import is_object_dict
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
        COALESCE((
            SELECT jsonb_object_agg(reported.condition, reported.age_seconds)
            FROM (
                SELECT e.payload ->> 'condition' AS condition,
                    EXTRACT(EPOCH FROM clock_timestamp() - max(e.created_at)) AS age_seconds
                FROM agent_impersonation_entries e
                WHERE e.lease_id = l.id AND e.kind = 'lifecycle'
                    AND e.payload ->> 'event' = 'event_log_incomplete'
                    AND e.payload ->> 'condition' IS NOT NULL
                GROUP BY 1
            ) reported
        ), '{}'::jsonb) AS reported_age
    FROM agent_impersonations l
    WHERE l.event_delivery_protocol_version = %s
        AND l.events_completed_at IS NULL
)
SELECT * FROM leases WHERE seal_stuck OR capture_failed
"""


# meiva:note
# One lease row of `_INCOMPLETE_LEASES`. `reported_age` is the jsonb object of seconds since each
# condition's last report.
@dataclass(frozen=True)
class _IncompleteLeaseRow:
    id: UUID
    agent_id: int
    session_id: int
    machine: str
    ended_at: datetime | None
    events_completed_at: datetime | None
    event_delivery_protocol_version: int | None
    event_delivery_pending_reason: str | None
    seal_stuck: bool
    capture_failed: bool
    pending_sources: list[str]
    reported_age: object


# meiva:note
# Report every lease and condition whose last report is older than `repeat_interval_seconds`, and
# return how many events that emitted. Record then emit inside the caller's transaction: the entry
# is what dates the report, and a rollback withdraws the claim instead of leaving a silent gap. A
# failure between the emit and the commit costs one repeated event, never a dropped one.
def emit_incomplete_event_logs(conn: psycopg.Connection, *, repeat_interval_seconds: int) -> int:
    leases = query_all(conn, _IncompleteLeaseRow, _INCOMPLETE_LEASES, (LOG_PROTOCOL_VERSION,))
    emitted = 0
    for lease in leases:
        reported_age = lease.reported_age
        if not is_object_dict(reported_age):
            raise TypeError("reported_age must be a JSON object")
        holding = {"seal_stuck": lease.seal_stuck, "capture_failed": lease.capture_failed}
        for condition in CONDITIONS:
            if not holding[condition]:
                continue
            age = reported_age.get(condition)
            if age is not None:
                if not isinstance(age, (int, float)):
                    raise TypeError("a reported age must be a number")
                if age < repeat_interval_seconds:
                    continue
            reason = str(
                pending_reason(
                    {
                        "events_completed_at": lease.events_completed_at,
                        "event_delivery_protocol_version": lease.event_delivery_protocol_version,
                        "event_delivery_pending_reason": lease.event_delivery_pending_reason,
                        "ended_at": lease.ended_at,
                    }
                )
                or "unknown"
            )
            append(
                conn,
                str(lease.id),
                "lifecycle",
                {
                    "event": "event_log_incomplete",
                    "condition": condition,
                    "pending_reason": reason,
                    "pending_sources": list(lease.pending_sources),
                    "lease_ended_at": (
                        None if lease.ended_at is None else lease.ended_at.isoformat()
                    ),
                },
            )
            logger.warning(
                "impersonation event log cannot complete: {condition} (lease {lease_id})",
                event="impersonation_event_log_incomplete",
                agent_id=lease.agent_id,
                condition=condition,
                lease_id=str(lease.id),
                lease_machine=lease.machine,
                pending_reason=reason,
                pending_sources=list(lease.pending_sources),
                session=f"{lease.agent_id}:{lease.session_id}",
            )
            emitted += 1
    return emitted

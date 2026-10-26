"""Log-native event recording (protocol v2): producers append event bodies to the lease's log.

The lease's own ``agent_impersonation_entries`` is the record. A source row is
appended in the caller's transaction (central producers) or at the capture seam
(a controller's receipt), and the database decides completeness from those rows
alone, so no external store is compared and no certifier takes part.

A central row holds a reference to the event's `audit_events` row (its
`event_uid` and line digest) instead of a second copy of the body; readers
resolve it through `history.entries`. A controller's SDK row, which is not an
audit event, still holds the body itself.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from base.agents.impersonation._store import lock_lease
from base.agents.impersonation.history import append, export_handoff
from base.agents.impersonation.status import LOG_PROTOCOL_VERSION
from base.telemetry import Event, event_id, event_line, event_line_digest
from base.telemetry.audit_events import audit_event_uid, record_audit
from base.telemetry.delivery.serialization import event_payload

# Cap on one lease's entries; reaching it fails capture rather than dropping a
# tail (about 1280 times the measured 78-event session maximum).
MAX_LOG_ENTRIES = 100_000
# Source key of rows appended by transaction-owning central producers.
CENTRAL_SOURCE = "central"


def event_item(event: Event) -> tuple[str, str, str, datetime]:
    """Return the event's stable key, full-byte digest, entry kind and timestamp."""
    line = event_line(event)
    timestamp_ns = int(event.ts.timestamp() * 1_000_000_000)
    key = f"event:{event_id(line, timestamp_ns)}"
    kind = "sdk_call" if event.event_name == "sdk_call" else "api_event"
    return key, event_line_digest(event), kind, event.ts


def locked_receipt_state(conn: psycopg.Connection, lease_id: str, source_key: str) -> str | None:
    """Lock one controller receipt and return its state (None when it does not exist)."""
    row = conn.execute(
        "SELECT lock_impersonation_event_participant(%s,%s)", (lease_id, source_key)
    ).fetchone()
    if row is None:
        raise RuntimeError("Receipt lock function returned no row")
    state = row[0]
    if state is not None and not isinstance(state, str):
        raise TypeError("Receipt lock state must be a string or None")
    return state


def append_source_event(
    conn: psycopg.Connection, lease: Mapping[str, Any], event: Event, *, source_key: str
) -> None:
    """Record one event in the lease's own log, in the caller's transaction.

    A central audit event is recorded in `audit_events` first (idempotent) and
    the entry carries only the reference to that row; any other event carries
    its body.
    """
    if lease["next_entry"] >= MAX_LOG_ENTRIES:
        raise RuntimeError("Impersonation event log reached its entry cap")
    line = event_line(event)
    key, digest, kind, timestamp = event_item(event)
    timestamp_ns = int(event.ts.timestamp() * 1_000_000_000)
    surrogate = event_id(line, timestamp_ns)
    if source_key == CENTRAL_SOURCE:
        record_audit(conn, event)
        payload: dict[str, Any] = {
            "event_uid": audit_event_uid(event),
            "id": surrogate,
            "line_sha256": digest,
        }
    else:
        payload = {**event_payload(event), "id": surrogate, "line_sha256": digest}
    append(
        conn,
        str(lease["id"]),
        kind,
        payload,
        event_key=key,
        created_at=timestamp,
        source_key=source_key,
    )


def refresh_completed_export(conn: psycopg.Connection, lease_id: str) -> None:
    """Rewrite an already delivered hand-off file once its event log completes."""
    lease = lock_lease(conn, lease_id)
    if lease["events_completed_at"] is not None and lease["handoff_path"] is not None:
        workspace = Path(str(lease["handoff_path"])).parents[1]
        document, _ = export_handoff(lease, conn, workspace=workspace)
        conn.execute(
            "UPDATE agent_impersonations SET handoff_document=%s WHERE id=%s",
            (Jsonb(document), lease_id),
        )


# meiva:note
# Seal the source receipts of ended leases whose controller never sealed them, and return how many
# receipts that sealed. A receipt is sealed by the controller process that opened it, on detach
# (`seal_local_participant`); a controller killed before its detach, or one whose seal was lost to
# a transient write failure, leaves the receipt open and no later pass is its owner, so the lease
# can never complete. Past `grace_seconds` after `ended_at` that receipt has no owner left: this
# pass writes `event_source_abandoned` into the lease's log (source key, open time, row count,
# reason) and seals it with the rows it holds, the count a graceful detach would have sealed, so
# the log completes with a declared gap instead of an unbounded one.
# meiva:link base/agents/impersonation/docs/event-log.meiva.okf.md
def seal_abandoned_event_sources(
    conn: psycopg.Connection, *, grace_seconds: int, limit: int
) -> int:
    candidates = conn.execute(
        "SELECT id FROM agent_impersonations WHERE event_delivery_protocol_version=%s "
        "AND ended_at IS NOT NULL "
        "AND ended_at <= clock_timestamp() - %s * interval '1 second' "
        "AND events_completed_at IS NULL "
        "AND EXISTS (SELECT 1 FROM agent_impersonation_event_participants p "
        "WHERE p.lease_id=agent_impersonations.id AND p.state='open') "
        "ORDER BY ended_at LIMIT %s",
        (LOG_PROTOCOL_VERSION, grace_seconds, limit),
    ).fetchall()
    sealed = 0
    for (lease_id,) in candidates:
        # meiva:note
        # The lease row lock is the serialization point every receipt writer takes before
        # touching a participant, so the states read below cannot change under this pass.
        lease = lock_lease(conn, str(lease_id))
        if lease["events_completed_at"] is not None:
            continue
        open_sources = conn.execute(
            "SELECT p.source_key, p.opened_at, "
            "(SELECT count(*) FROM agent_impersonation_entries e "
            "WHERE e.lease_id=p.lease_id AND e.source_key=p.source_key) AS item_count "
            "FROM agent_impersonation_event_participants p "
            "WHERE p.lease_id=%s AND p.state='open' ORDER BY p.opened_at",
            (lease_id,),
        ).fetchall()
        for source_key, opened_at, item_count in open_sources:
            append(
                conn,
                str(lease_id),
                "lifecycle",
                {
                    "event": "event_source_abandoned",
                    "source_key": source_key,
                    "source_opened_at": opened_at.isoformat(),
                    "item_count": item_count,
                    "grace_seconds": grace_seconds,
                    "reason": "the controller process never sealed this source receipt",
                },
                event_key=f"abandoned:{source_key}",
            )
            # meiva:note
            # The seal procedure re-counts under the receipt lock and refuses a count that
            # differs, so a source whose rows moved cannot be sealed from this count alone.
            conn.execute(
                "SELECT seal_impersonation_event_participant(%s,%s,'sealed',NULL,"
                "(SELECT count(*) FROM agent_impersonation_entries "
                "WHERE lease_id=%s AND source_key=%s))",
                (lease_id, source_key) * 2,
            )
            sealed += 1
        if open_sources:
            refresh_completed_export(conn, str(lease_id))
    return sealed

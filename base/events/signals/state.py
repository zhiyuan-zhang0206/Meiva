# meiva:note
# The state of a condition, as opposed to the record of a check.
#
# Every detector in the cluster answers "is this true right now" by writing an event, and an
# event cannot be un-written: 1,877 OTLP export refusals in one day are 1,877 rows that all say
# the same thing, and nothing in the database says whether the condition still holds. This
# module owns the one row that does -- one open episode per (source, subject, condition),
# touched while the condition persists and resolved when it stops.
#
# It does not replace `base/events/alarms`: that records what a detector told someone, on one
# machine, and it works with the database gone. This one records what is still true across the
# cluster, and it needs the database. A detector that wants both records the alarm first
# (durable evidence of the detection) and touches a signal second (the open state).
# meiva:link base/events/signals/docs/signals.meiva.okf.md

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, LiteralString

import psycopg
from psycopg.rows import dict_row

__all__ = ["Episode", "ResolveReason", "SignalSeverity", "open_or_touch", "resolve"]


# meiva:note
# The three grades a condition can be reported at. The database repeats this list in a CHECK
# constraint and `tests/contracts/test_db_check_enum_sync.py` compares the two, so a new grade
# costs one edit here and one migration.
class SignalSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


# meiva:note
# Why an episode closed. The database stores the text instead of testing it against a list:
# every condition grows its own reasons over time, and a CHECK would turn each new one into a
# migration. This enum is the vocabulary the write path accepts today.
class ResolveReason(StrEnum):
    RECOVERED = "recovered"
    TTL_EXPIRED_UNVERIFIED = "ttl_expired_unverified"
    ACKED_BY_OWNER = "acked_by_owner"


# meiva:note
# One row of `signal_state` as callers read it: identity, the newest reading, and how the
# episode ended (or that it has not). `count` is how many reports it absorbed; the reports
# themselves are in `telemetry_events`, pointed at by `first_event_id` and `last_event_id`.
@dataclass(frozen=True)
class Episode:
    id: int
    source: str
    subject: str
    condition: str
    severity: SignalSeverity
    opened_at: datetime
    last_seen: datetime
    count: int
    first_event_id: int | None
    last_event_id: int | None
    resolved_at: datetime | None
    resolve_reason: str | None
    evidence_ref: str | None
    acked_by: int | None
    acked_at: datetime | None


# meiva:note
# The conflict target repeats the predicate of `signal_state_open_key_idx`, because PostgreSQL
# only infers a partial index when the statement names its predicate. A resolved row is not a
# conflict: the next report opens a new episode and the closed one stays as history. The newest
# report also decides the severity the row shows -- a condition that escalated is described by
# its last reading, and the history of how it got there is in the events.
# meiva:note
# `RETURNING *` rather than a column list: the reader below maps by name, so a column added by a
# later migration arrives here without a second list to keep in step.
_TOUCH = """
INSERT INTO signal_state
    (source, subject, condition, severity, first_event_id, last_event_id)
VALUES
    (%(source)s, %(subject)s, %(condition)s, %(severity)s, %(event_id)s, %(event_id)s)
ON CONFLICT (source, subject, condition) WHERE resolved_at IS NULL
DO UPDATE SET
    last_seen = now(),
    count = signal_state.count + 1,
    severity = EXCLUDED.severity,
    last_event_id = COALESCE(EXCLUDED.last_event_id, signal_state.last_event_id)
RETURNING *
"""

# meiva:note
# Closing is defined on the open row only, so resolving twice is a no-op that returns nothing
# rather than an error: a reconciler may re-derive the same verdict from a stale read, and that
# race is ordinary, not a fault.
_RESOLVE = """
UPDATE signal_state SET
    resolved_at = now(),
    resolve_reason = %(reason)s,
    evidence_ref = %(evidence_ref)s
WHERE source = %(source)s
  AND subject = %(subject)s
  AND condition = %(condition)s
  AND resolved_at IS NULL
RETURNING *
"""


# meiva:note
# Rows are read through a mapping cursor this module owns, not through whatever factory the
# caller configured: a database surface must not change shape with its caller's connection.
def _episode(row: Mapping[str, Any]) -> Episode:
    return Episode(
        id=row["id"],
        source=row["source"],
        subject=row["subject"],
        condition=row["condition"],
        severity=SignalSeverity(row["severity"]),
        opened_at=row["opened_at"],
        last_seen=row["last_seen"],
        count=row["count"],
        first_event_id=row["first_event_id"],
        last_event_id=row["last_event_id"],
        resolved_at=row["resolved_at"],
        resolve_reason=row["resolve_reason"],
        evidence_ref=row["evidence_ref"],
        acked_by=row["acked_by"],
        acked_at=row["acked_at"],
    )


def _one(
    conn: psycopg.Connection[Any], statement: LiteralString, params: Mapping[str, Any]
) -> Episode | None:
    with conn.cursor(row_factory=dict_row) as cursor:
        cursor.execute(statement, params)
        row = cursor.fetchone()
    return None if row is None else _episode(row)


# meiva:note
# The one write a detector makes: report the condition. `event_id` is optional because a caller
# may touch a signal for a reading it did not record as an event -- but when it passes one, that
# event becomes the newest evidence and the first one is kept as the episode's origin.
def open_or_touch(
    conn: psycopg.Connection[Any],
    *,
    source: str,
    subject: str,
    condition: str,
    severity: SignalSeverity,
    event_id: int | None = None,
) -> Episode:
    params: dict[str, Any] = {
        "source": source,
        "subject": subject,
        "condition": condition,
        "severity": str(severity),
        "event_id": event_id,
    }
    episode = _one(conn, _TOUCH, params)
    if episode is None:
        raise RuntimeError("signal_state upsert returned no row")
    return episode


# meiva:note
# The one write a resolver makes: the condition is gone. `None` means there was nothing open to
# close -- a re-derived verdict arriving after someone else closed it, which is a no-op.
def resolve(
    conn: psycopg.Connection[Any],
    *,
    source: str,
    subject: str,
    condition: str,
    reason: ResolveReason | str,
    evidence_ref: str | None = None,
) -> Episode | None:
    params: dict[str, Any] = {
        "source": source,
        "subject": subject,
        "condition": condition,
        "reason": str(reason),
        "evidence_ref": evidence_ref,
    }
    return _one(conn, _RESOLVE, params)

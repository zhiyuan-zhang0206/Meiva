# meiva:note
# The escalation pass: which open conditions deserve a person, and what the ledger remembers.
#
# It never copies a condition: severity, whether the alert still fires and when it started are
# read from the source row on every pass, so the ledger cannot disagree with the truth. Two
# rules follow from that and are the whole contract:
#
# - an unreachable source means this round does nothing. Falling back to the previous pass's
#   answer would turn "the database is gone" into "the condition still fires", which is how a
#   silence becomes an event.
# - only this pass writes the ledger. The ingest and the IM leg read it; a second writer would
#   put two answers to "was a person told" in one table.
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

import psycopg
from psycopg_pool import ConnectionPool

from base.db.transaction import write_transaction

__all__ = [
    "Candidate",
    "EscalationMode",
    "due_escalations",
    "record_shadow",
    "run_escalation_pass",
]

_log = logging.getLogger(__name__)

# meiva:note
# The severities whose open episodes are candidates for a person. Grafana writes `critical`,
# `error` and `warning`; `warning` is the everyday band that a digest can carry later, so it is
# not a candidate here. When the class whitelist lands, it replaces this list rather than
# sitting beside it — two rules for "who gets woken" is the defect, not the list.
ESCALATION_SEVERITIES = ("critical", "error")

# meiva:note
# How many episodes one pass considers. The bound exists so a fleet-wide outage cannot turn one
# tick into an unbounded query; the oldest episodes are taken first, which is the order a person
# would want them in.
MAX_ESCALATIONS_PER_PASS = 20


class EscalationMode(StrEnum):
    # meiva:note
    # Whether the pass decides only (shadow) or also delivers. Live is not implemented yet, so it
    # is not a value here: a selectable mode that silently does nothing is worse than its absence.

    OFF = "off"
    SHADOW = "shadow"


@dataclass(frozen=True)
class Candidate:
    # meiva:note
    # One open episode old enough to escalate, with the facts read from the source row this pass.

    source_id: int
    alertname: str
    severity: str
    age_s: float


def due_escalations(
    conn: psycopg.Connection,
    *,
    now: datetime,
    min_age_s: float,
    limit: int = MAX_ESCALATIONS_PER_PASS,
) -> list[Candidate]:
    # meiva:note
    # Open episodes past their age floor whose ledger row is not cooling down. The condition facts
    # come from `alerts` in this query, which is what keeps the ledger from disagreeing with them.
    rows = conn.execute(
        """
        SELECT a.id, a.alertname, a.severity,
               EXTRACT(EPOCH FROM (%(now)s - a.starts_at)) AS age_s
        FROM alerts a
        LEFT JOIN alert_escalations e
               ON e.source_kind = 'alerts' AND e.source_id = a.id
        WHERE a.status <> 'resolved'
          AND a.severity = ANY(%(severities)s)
          AND a.starts_at <= %(now)s - make_interval(secs => %(min_age_s)s)
          AND (e.cooldown_until IS NULL OR e.cooldown_until <= %(now)s)
        ORDER BY a.starts_at
        LIMIT %(limit)s
        """,
        {
            "now": now,
            "severities": list(ESCALATION_SEVERITIES),
            "min_age_s": float(min_age_s),
            "limit": limit,
        },
    ).fetchall()
    return [Candidate(row[0], row[1], row[2], float(row[3])) for row in rows]


def record_shadow(
    conn: psycopg.Connection,
    candidates: list[Candidate],
    *,
    now: datetime,
    cooldown_s: float,
) -> int:
    # meiva:note
    # Record that these episodes would have escalated, without claiming a delivery: the shadow
    # counter grows, the delivered counter does not.
    cooldown_until = now + timedelta(seconds=cooldown_s)
    for candidate in candidates:
        conn.execute(
            """
            INSERT INTO alert_escalations
                (source_kind, source_id, shadow_escalations, last_delivery_at,
                 last_delivery_result, cooldown_until, updated_at)
            VALUES ('alerts', %(source_id)s, 1, %(now)s, 'shadow', %(cooldown_until)s, %(now)s)
            ON CONFLICT (source_kind, source_id) DO UPDATE
               SET shadow_escalations = alert_escalations.shadow_escalations + 1,
                   last_delivery_at = EXCLUDED.last_delivery_at,
                   last_delivery_result = EXCLUDED.last_delivery_result,
                   cooldown_until = EXCLUDED.cooldown_until,
                   updated_at = EXCLUDED.updated_at
            """,
            {
                "source_id": candidate.source_id,
                "now": now,
                "cooldown_until": cooldown_until,
            },
        )
    return len(candidates)


def run_escalation_pass(
    pool: ConnectionPool,
    *,
    mode: EscalationMode,
    now: datetime,
    min_age_s: float,
    cooldown_s: float,
    limit: int = MAX_ESCALATIONS_PER_PASS,
) -> int:
    # meiva:note
    # One tick: decide, then (shadow) record what a person would have been told. Returns how many
    # episodes were decided. An unreachable database propagates to the caller, which owns the
    # loop's retry policy; nothing is decided from a stale reading.
    if mode is EscalationMode.OFF:
        return 0
    with pool.connection() as conn:
        due = due_escalations(conn, now=now, min_age_s=min_age_s, limit=limit)
    if not due:
        return 0
    for candidate in due:
        _log.warning(
            "[events-maintenance] escalation would page a person: alert=%s id=%s severity=%s "
            "open_for=%.0fs mode=%s",
            candidate.alertname,
            candidate.source_id,
            candidate.severity,
            candidate.age_s,
            mode.value,
        )
    if mode is EscalationMode.SHADOW:
        with write_transaction(pool) as conn:
            return record_shadow(conn, due, now=now, cooldown_s=cooldown_s)
    return len(due)

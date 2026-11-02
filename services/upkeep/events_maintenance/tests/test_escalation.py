# meiva:note
# The escalation ledger and the pass that fills it, decided from episodes and a clock.
#
# Every case seeds real alert rows and reads the ledger back, because the pass's whole job is a
# query plus an upsert: a fake would test the fake. The cases that matter are the four ways a
# decision must NOT happen — too young, already resolved, cooling down, or the source gone.

# meiva:note
# Every subprocess-free call here runs against the test database this fixture provisions, so S603
# has no untrusted command to flag.
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from psycopg_pool import PoolClosed

import base.db
from base.db.code_version_gate import ProcessDbGate
from services.upkeep.events_maintenance.escalation import (
    EscalationMode,
    due_escalations,
    run_escalation_pass,
)

NOW = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)


def _insert_alert(
    conn: psycopg.Connection,
    *,
    starts_at: datetime,
    severity: str = "critical",
    status: str = "unresolved",
    fingerprint: str = "fp-1",
) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO alerts"
            " (status, severity, alertname, labels, annotations, starts_at, ends_at,"
            "  fingerprint, source, updated_at)"
            " VALUES (%s, %s, 'test-alert', '{}', '{}', %s, NULL, %s, 'grafana', %s)"
            " RETURNING id",
            (status, severity, starts_at, fingerprint, starts_at),
        )
        row = cur.fetchone()
    assert row is not None
    conn.commit()
    return row[0]


def _ledger(conn: psycopg.Connection, source_id: int) -> tuple[int, int, str | None] | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT escalation_count, shadow_escalations, last_delivery_result"
            " FROM alert_escalations WHERE source_kind='alerts' AND source_id=%s",
            (source_id,),
        )
        return cur.fetchone()


def test_an_episode_younger_than_the_floor_is_not_due(db_conn: psycopg.Connection) -> None:
    _insert_alert(db_conn, starts_at=NOW - timedelta(seconds=120))

    assert due_escalations(db_conn, now=NOW, min_age_s=600) == []


def test_a_persisting_episode_becomes_due_with_its_own_age(db_conn: psycopg.Connection) -> None:
    alert_id = _insert_alert(db_conn, starts_at=NOW - timedelta(seconds=900))

    due = due_escalations(db_conn, now=NOW, min_age_s=600)

    assert [(c.source_id, c.severity) for c in due] == [(alert_id, "critical")]
    assert 890 <= due[0].age_s <= 910


def test_a_resolved_episode_is_never_due(db_conn: psycopg.Connection) -> None:
    _insert_alert(db_conn, starts_at=NOW - timedelta(hours=2), status="resolved")

    assert due_escalations(db_conn, now=NOW, min_age_s=600) == []


def test_a_warning_is_not_a_candidate(db_conn: psycopg.Connection) -> None:
    _insert_alert(db_conn, starts_at=NOW - timedelta(hours=2), severity="warning")

    assert due_escalations(db_conn, now=NOW, min_age_s=600) == []


def test_shadow_records_the_decision_without_claiming_a_delivery(
    db_conn: psycopg.Connection, database_gate: ProcessDbGate
) -> None:
    alert_id = _insert_alert(db_conn, starts_at=NOW - timedelta(minutes=15))
    pool = base.db.pool(gate=database_gate)
    try:
        decided = run_escalation_pass(
            pool, mode=EscalationMode.SHADOW, now=NOW, min_age_s=600, cooldown_s=1800
        )
    finally:
        pool.close()

    assert decided == 1
    assert _ledger(db_conn, alert_id) == (0, 1, "shadow")


def test_the_cooldown_on_the_ledger_holds_across_restarts(
    db_conn: psycopg.Connection, database_gate: ProcessDbGate
) -> None:
    alert_id = _insert_alert(db_conn, starts_at=NOW - timedelta(minutes=15))
    pool = base.db.pool(gate=database_gate)
    try:
        run_escalation_pass(
            pool, mode=EscalationMode.SHADOW, now=NOW, min_age_s=600, cooldown_s=1800
        )
        # meiva:note
        # A restart is a new process with a new pool, so the wait must live on the row rather than
        # in memory: the second decision is taken against the stored cooldown.
        again = run_escalation_pass(
            pool,
            mode=EscalationMode.SHADOW,
            now=NOW + timedelta(minutes=5),
            min_age_s=600,
            cooldown_s=1800,
        )
        later = run_escalation_pass(
            pool,
            mode=EscalationMode.SHADOW,
            now=NOW + timedelta(minutes=31),
            min_age_s=600,
            cooldown_s=1800,
        )
    finally:
        pool.close()

    assert (again, later) == (0, 1)
    assert _ledger(db_conn, alert_id) == (0, 2, "shadow")


def test_off_decides_nothing_and_writes_nothing(
    db_conn: psycopg.Connection, database_gate: ProcessDbGate
) -> None:
    alert_id = _insert_alert(db_conn, starts_at=NOW - timedelta(hours=2))
    pool = base.db.pool(gate=database_gate)
    try:
        decided = run_escalation_pass(
            pool, mode=EscalationMode.OFF, now=NOW, min_age_s=600, cooldown_s=1800
        )
    finally:
        pool.close()

    assert decided == 0
    assert _ledger(db_conn, alert_id) is None


def test_a_source_that_cannot_be_read_decides_nothing(
    db_conn: psycopg.Connection, database_gate: ProcessDbGate
) -> None:
    # meiva:note
    # The pool is closed before the pass runs: the round must raise for its loop to own the retry,
    # and it must not decide from the last reading it saw.
    alert_id = _insert_alert(db_conn, starts_at=NOW - timedelta(hours=2))
    pool = base.db.pool(gate=database_gate)
    pool.close()

    with pytest.raises(PoolClosed):
        run_escalation_pass(
            pool, mode=EscalationMode.SHADOW, now=NOW, min_age_s=600, cooldown_s=1800
        )

    assert _ledger(db_conn, alert_id) is None

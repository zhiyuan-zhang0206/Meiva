# meiva:note
# What the write path has to guarantee: one open row per condition, a closed row that stays as
# history, and no commit of its own -- the caller's transaction decides. Each test names the
# mechanism it proves, not a symptom it happened to see.
# meiva:link base/events/signals/docs/signals.meiva.okf.md

from __future__ import annotations

import uuid
from typing import Any

import psycopg
import pytest

from base.events.signals import ResolveReason, SignalSeverity, open_or_touch, resolve


def _key() -> tuple[str, str, str]:
    suffix = uuid.uuid4().hex[:12]
    return (f"test-source-{suffix}", f"test-subject-{suffix}", "test-condition")


def _rows(conn: psycopg.Connection[Any], key: tuple[str, str, str]) -> list[tuple[Any, ...]]:
    return conn.execute(
        "SELECT id, count, severity, resolved_at, resolve_reason, evidence_ref, first_event_id,"
        " last_event_id FROM signal_state WHERE source = %s AND subject = %s AND condition = %s"
        " ORDER BY id",
        key,
    ).fetchall()


def test_repeat_touch_keeps_one_row_and_counts(db_conn: psycopg.Connection[Any]) -> None:
    source, subject, condition = _key()

    first = open_or_touch(
        db_conn,
        source=source,
        subject=subject,
        condition=condition,
        severity=SignalSeverity.WARNING,
        event_id=101,
    )
    second = open_or_touch(
        db_conn,
        source=source,
        subject=subject,
        condition=condition,
        severity=SignalSeverity.WARNING,
        event_id=102,
    )

    assert second.id == first.id
    assert (second.count, first.count) == (2, 1)
    assert len(_rows(db_conn, (source, subject, condition))) == 1


def test_the_origin_event_stays_and_the_newest_one_moves(db_conn: psycopg.Connection[Any]) -> None:
    source, subject, condition = _key()

    open_or_touch(
        db_conn,
        source=source,
        subject=subject,
        condition=condition,
        severity=SignalSeverity.WARNING,
        event_id=101,
    )
    touched = open_or_touch(
        db_conn,
        source=source,
        subject=subject,
        condition=condition,
        severity=SignalSeverity.ERROR,
        event_id=102,
    )
    without_evidence = open_or_touch(
        db_conn,
        source=source,
        subject=subject,
        condition=condition,
        severity=SignalSeverity.ERROR,
    )

    assert (touched.first_event_id, touched.last_event_id) == (101, 102)
    assert touched.severity is SignalSeverity.ERROR
    assert without_evidence.last_event_id == 102


def test_the_newest_report_decides_the_severity(db_conn: psycopg.Connection[Any]) -> None:
    source, subject, condition = _key()

    open_or_touch(
        db_conn,
        source=source,
        subject=subject,
        condition=condition,
        severity=SignalSeverity.ERROR,
    )
    touched = open_or_touch(
        db_conn,
        source=source,
        subject=subject,
        condition=condition,
        severity=SignalSeverity.INFO,
    )

    assert touched.severity is SignalSeverity.INFO


def test_a_second_open_row_is_refused_by_the_index(db_conn: psycopg.Connection[Any]) -> None:
    source, subject, condition = _key()
    open_or_touch(
        db_conn,
        source=source,
        subject=subject,
        condition=condition,
        severity=SignalSeverity.WARNING,
    )

    with pytest.raises(psycopg.errors.UniqueViolation), db_conn.transaction():
        db_conn.execute(
            "INSERT INTO signal_state (source, subject, condition, severity)"
            " VALUES (%s, %s, %s, 'warning')",
            (source, subject, condition),
        )


def test_a_resolved_condition_reopens_as_a_new_episode(db_conn: psycopg.Connection[Any]) -> None:
    source, subject, condition = _key()
    first = open_or_touch(
        db_conn,
        source=source,
        subject=subject,
        condition=condition,
        severity=SignalSeverity.WARNING,
    )

    closed = resolve(
        db_conn,
        source=source,
        subject=subject,
        condition=condition,
        reason=ResolveReason.RECOVERED,
        evidence_ref="log line 12: export accepted",
    )
    reopened = open_or_touch(
        db_conn,
        source=source,
        subject=subject,
        condition=condition,
        severity=SignalSeverity.WARNING,
    )

    assert closed is not None
    assert (closed.id, closed.resolved_at is not None) == (first.id, True)
    assert closed.resolve_reason == "recovered"
    assert closed.evidence_ref == "log line 12: export accepted"
    assert reopened.id != first.id
    assert reopened.count == 1
    assert len(_rows(db_conn, (source, subject, condition))) == 2


def test_resolving_a_closed_episode_is_a_no_op(db_conn: psycopg.Connection[Any]) -> None:
    source, subject, condition = _key()
    open_or_touch(
        db_conn,
        source=source,
        subject=subject,
        condition=condition,
        severity=SignalSeverity.WARNING,
    )
    first = resolve(
        db_conn,
        source=source,
        subject=subject,
        condition=condition,
        reason=ResolveReason.RECOVERED,
    )

    again = resolve(
        db_conn,
        source=source,
        subject=subject,
        condition=condition,
        reason=ResolveReason.ACKED_BY_OWNER,
    )

    assert first is not None
    assert again is None
    rows = _rows(db_conn, (source, subject, condition))
    assert len(rows) == 1
    assert rows[0][4] == "recovered"


def test_resolving_something_that_never_opened_is_a_no_op(
    db_conn: psycopg.Connection[Any],
) -> None:
    source, subject, condition = _key()

    assert (
        resolve(
            db_conn,
            source=source,
            subject=subject,
            condition=condition,
            reason=ResolveReason.RECOVERED,
        )
        is None
    )


def test_a_touch_is_not_committed_by_this_module(db_conn: psycopg.Connection[Any]) -> None:
    source, subject, condition = _key()

    with pytest.raises(RuntimeError), db_conn.transaction():
        open_or_touch(
            db_conn,
            source=source,
            subject=subject,
            condition=condition,
            severity=SignalSeverity.WARNING,
        )
        raise RuntimeError("the caller gives up half way")

    assert _rows(db_conn, (source, subject, condition)) == []

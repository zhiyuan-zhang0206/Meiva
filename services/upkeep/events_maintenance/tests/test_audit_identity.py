# meiva:note
# The audit-identity pass (task #6235): rows whose writer could not name its machine.
#
# `#6089` made `audit_events.machine` a canonical name or the literal `unknown`, which turned a lost
# identity into a readable value. This pass is the reader: a row written inside the window is reported
# with the split count and the day total, a row that names its machine is not counted, a row older than
# the window reaches the day book without firing, and a pass that saw nothing says nothing.
#
# The instant is fixed far from live data, so each case states its numbers instead of comparing against
# whatever else the table holds: `audit_events` is append-only, so no test can clear it.


from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from psycopg_pool import ConnectionPool

import base.db
from base.db.code_version_gate import ProcessDbGate
from base.telemetry.tests.capture.event_capture import EmittedEvents
from base.telemetry.tests.capture.event_capture import emitted_events as emitted_events
from services.upkeep.events_maintenance import audit_identity

# meiva:note
# `WINDOW` is the window the daemon derives (one maintenance interval plus one pass deadline), and
# `NOW` is far enough ahead that no live row shares its window or its day.
WINDOW = 300.0
NOW = datetime(2030, 1, 1, 12, 0, tzinfo=UTC)


@pytest.fixture
def pool(db_url: str, *, database_gate: ProcessDbGate) -> Iterator[ConnectionPool]:
    p = base.db.pool(max_size=2, gate=database_gate)
    try:
        yield p
    finally:
        p.close()


# meiva:note
# One audit row around the fixed instant: the pass reads the table, and the writer that would
# normally fill it (the emitter with a machine reader) is not part of what this test exercises.
def _audit_row(
    db_conn: psycopg.Connection,
    *,
    machine: str,
    agent_id: int | None = None,
    seconds_before_now: int = 5,
) -> None:
    db_conn.execute(
        "INSERT INTO audit_events "
        "(event_uid, ts, agent_id, machine, process, event_name, level, source) "
        "VALUES (%s, %s, %s, %s, 'worker', 'skill_invoked', 'info', 'agent')",
        (
            uuid.uuid4().int & ((1 << 63) - 1),
            NOW - timedelta(seconds=seconds_before_now),
            agent_id,
            machine,
        ),
    )
    db_conn.commit()


def test_a_row_whose_writer_could_not_name_its_machine_is_reported(
    pool: ConnectionPool,
    db_conn: psycopg.Connection,
    emitted_events: EmittedEvents,
) -> None:
    _audit_row(db_conn, machine="unknown")
    _audit_row(db_conn, machine="unknown")
    _audit_row(db_conn, machine="unknown", agent_id=4242)

    audit_identity.report_unknown_writers(pool, window_seconds=WINDOW, now=NOW)

    assert emitted_events.attributes("audit_machine_unknown") == [
        {"user_rows": 2, "agent_rows": 1, "window_rows": 3, "day_rows": 3, "window_seconds": 300}
    ]


# meiva:note
# A named machine is the invariant working, not a reading: counting it would make the event fire for
# every audit row the cluster writes.
def test_a_row_that_names_its_machine_is_not_counted(
    pool: ConnectionPool,
    db_conn: psycopg.Connection,
    emitted_events: EmittedEvents,
) -> None:
    _audit_row(db_conn, machine="macmini", agent_id=4242)

    audit_identity.report_unknown_writers(pool, window_seconds=WINDOW, now=NOW)

    assert emitted_events.attributes("audit_machine_unknown") == []


# meiva:note
# The two halves of the reading answer different questions: a row older than the window was already
# reported by the pass that saw it, and the day total still counts it. This is the test that keeps
# the window from degenerating into "the last 24 hours", which would re-report one row all day.
def test_a_row_older_than_the_window_reaches_the_day_book_without_firing(
    pool: ConnectionPool,
    db_conn: psycopg.Connection,
    emitted_events: EmittedEvents,
) -> None:
    _audit_row(db_conn, machine="unknown", seconds_before_now=7200)

    rows = audit_identity.read_unknown_writers(db_conn, window_seconds=WINDOW, now=NOW)
    audit_identity.report_unknown_writers(pool, window_seconds=WINDOW, now=NOW)

    # meiva:note
    # `user_rows` and `agent_rows` split the window, not the day: the day total carries the row, the
    # split stays empty because nothing in the window was unnamed.
    assert (rows.window_rows, rows.day_rows, rows.user_rows, rows.agent_rows) == (0, 1, 0, 0)
    assert emitted_events.attributes("audit_machine_unknown") == []


def test_a_pass_that_saw_no_unnamed_writer_emits_nothing(
    pool: ConnectionPool,
    emitted_events: EmittedEvents,
) -> None:
    audit_identity.report_unknown_writers(pool, window_seconds=WINDOW, now=NOW)

    assert emitted_events.attributes("audit_machine_unknown") == []

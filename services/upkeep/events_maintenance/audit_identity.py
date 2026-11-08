# meiva:note
# Audit rows whose writer could not name its machine (task #6235), a pass of the
# events-maintenance service.
#
# `#6089` made `audit_events.machine` take one of two forms: a canonical machine name, or the literal
# `unknown` when the writing process could not resolve its own. Before that the column fell back to a
# hostname, so a writer that lost its identity still produced a plausible value; the invariant made
# the loss readable, and nothing read it -- a silent substitution became a silent degradation.
#
# Two numbers, one statement:
#
# - the trailing window (`window_seconds`: one maintenance interval plus the pass deadline, so a slow
#   pass cannot open a gap between two counts) counts the rows written since the previous pass. A
#   cluster that loses no identity reads zero, which is what makes a non-zero reading worth an event.
# - the trailing 24-hour total is what a day book asks -- how many rows today do not say who wrote
#   them. It rides along as context and is not a second trigger: read on its own it stays non-zero for
#   a day after a single row, and a number that is usually non-zero teaches nothing.
#
# `user_rows` and `agent_rows` split the window count by whether the row carries an agent: both mean
# the writer could not resolve its machine, and only the second one means a running agent process did.
#
# The instant is a parameter, not `now()`: the daemon threads the one clock its pass already uses
# through every time-based step, and a caller that fixes the instant can state what the count is
# without depending on what else the table holds.


from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import psycopg
from psycopg_pool import ConnectionPool

from base import telemetry
from base.db.rows import query_one

# meiva:note
# The view, not the table: `audit_events_canonical` is the single read path for the machine
# dimension (task #6089), so a historical hostname is mapped in exactly one place. A row whose
# machine cannot be named keeps the literal `unknown` there, which is the value this pass counts.
_UNKNOWN_SQL = """
    SELECT
        count(*) FILTER (WHERE agent_id IS NULL)::bigint AS user_rows,
        count(*) FILTER (WHERE agent_id IS NOT NULL)::bigint AS agent_rows,
        count(*)::bigint AS window_rows,
        (
            SELECT count(*)
            FROM audit_events_canonical
            WHERE machine_canonical = 'unknown'
              AND ts >= %(now)s - interval '24 hours'
        )::bigint AS day_rows
    FROM audit_events_canonical
    WHERE machine_canonical = 'unknown'
      AND ts >= %(now)s - make_interval(secs => %(window_seconds)s)
"""


# meiva:note
# One pass's reading, named as the statement selects it.
@dataclass(frozen=True)
class _UnknownRows:
    user_rows: int
    agent_rows: int
    window_rows: int
    day_rows: int


# meiva:note
# The reading itself, blocking: the caller runs it in the pass's worker thread, beside the rollup it
# shares a connection pool with. `window_seconds` is the caller's, because only the daemon knows how
# often it sweeps and how long one pass may run.
def read_unknown_writers(
    conn: psycopg.Connection, *, window_seconds: float, now: datetime
) -> _UnknownRows:
    row = query_one(
        conn,
        _UnknownRows,
        _UNKNOWN_SQL,
        {"now": now, "window_seconds": window_seconds},
    )
    if row is None:
        raise RuntimeError("audit_identity: the count of unnamed writers returned no row")
    return row


# meiva:note
# One event per pass that saw a row the writer could not attribute, and nothing while the count is
# zero -- an event emitted every pass would make the reading unreadable by being always there.
def report_unknown_writers(pool: ConnectionPool, *, window_seconds: float, now: datetime) -> None:
    with pool.connection() as conn:
        rows = read_unknown_writers(conn, window_seconds=window_seconds, now=now)
    if rows.window_rows == 0:
        return
    telemetry.emit(
        "telemetry",
        "audit_machine_unknown",
        attributes={
            "user_rows": rows.user_rows,
            "agent_rows": rows.agent_rows,
            "window_rows": rows.window_rows,
            "day_rows": rows.day_rows,
            "window_seconds": int(window_seconds),
        },
    )

# meiva:note
# The two liveness reports the schedule manager emits, both read off the schedule's own round
# heartbeat rather than off the manager's bookkeeping.
#
# `report_late_rounds` answers "the session is live, is the loop still iterating": a resident
# schedule keeps its session while it wedges inside a round, and its own heartbeat says when it
# expected to be back.
#
# `report_stalled` answers "the session is gone, how long has the work been stopped": a manager
# can notice a lost session hours late, so the reported silence is measured from the last round
# the schedule actually finished, not from the moment the manager noticed.
#
# The clock comes from Postgres rather than this process: the manager runs under the strict
# profile and may not read a clock itself, and the database is the same wall clock the file
# mtimes follow.

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from psycopg_pool import ConnectionPool

from base import telemetry
from base.daemon.schedules.heartbeat import (
    Beat,
    is_overdue,
    read_alert_marks,
    read_beat,
    write_alert_marks,
)
from base.db.rows import query_all, query_one
from base.db.transaction import write_transaction
from base.deploy.schedule_timing import SCHEDULE_PROGRESS_SKEW_S, SCHEDULE_STALL_ALERT_AFTER_S

__all__ = ["report_late_rounds", "report_stalled"]

STALL_ALERT_AFTER_S = SCHEDULE_STALL_ALERT_AFTER_S

_log = logging.getLogger(__name__)


def report_late_rounds(
    pool: ConnectionPool,
    *,
    home: Path,
    status_by_id: Mapping[int, str],
    live_ids: set[int],
) -> None:
    beats = {sid: read_beat(home, sid) for sid in status_by_id if sid in live_ids}
    if not any(beat is not None for beat in beats.values()):
        return
    marks = read_alert_marks(home)
    changed = False
    now = _database_clock(pool)
    for schedule_id, status in sorted(status_by_id.items()):
        key = str(schedule_id)
        beat = beats.get(schedule_id)
        if beat is None or not is_overdue(beat, now=now, skew_s=SCHEDULE_PROGRESS_SKEW_S):
            if marks.pop(key, None) is not None:
                changed = True
            continue
        marker = beat.alive_by.isoformat(timespec="seconds")
        if marks.get(key) == marker:
            continue
        marks[key] = marker
        changed = True
        _emit(schedule_id, status, beat, now)
    if changed:
        write_alert_marks(home, marks)


def _emit(schedule_id: int, status: str, beat: Beat, now: datetime) -> None:
    overdue_seconds = round((now - beat.alive_by).total_seconds(), 3)
    marker = beat.alive_by.isoformat(timespec="seconds")
    _log.warning(
        "schedule %s: no round started by %s (%.1fs late, status=%s)",
        schedule_id,
        marker,
        overdue_seconds,
        status,
    )
    telemetry.emit(
        "telemetry",
        event_name="schedule_not_progressing",
        level="warning",
        attributes={
            "schedule_id": schedule_id,
            "status": status,
            "overdue_seconds": overdue_seconds,
            "alive_by": marker,
        },
    )


@dataclass(frozen=True)
class _StallRow:
    id: int
    not_live_since: datetime
    observed_at: datetime


# meiva:note
# ``not_live_since`` and ``stall_alerted_at`` are columns of the row, so one outage alerts once
# even across restarts, and a live, completed or disabled schedule clears both -- which rearms a
# later outage.
#
# What it reports is the silence, not the detection. A resident schedule's own beat names the
# last round it finished, so a manager that noticed the lost session three hours late still says
# the work has been stopped for three hours. Without a beat the only observable fact is the
# sessionless observation itself, and ``silent_seconds`` falls back to it.
def report_stalled(
    pool: ConnectionPool,
    *,
    machine: str,
    home: Path,
    status_by_id: Mapping[int, str],
    live_ids: set[int],
) -> None:
    eligible = {sid for sid, status in status_by_id.items() if status != "completed"}
    missing = sorted(eligible - live_ids)
    with write_transaction(pool) as conn:
        conn.execute(
            "UPDATE schedules SET not_live_since = NULL, stall_alerted_at = NULL "
            "WHERE machine = %s AND NOT (id = ANY(%s)) "
            "AND (not_live_since IS NOT NULL OR stall_alerted_at IS NOT NULL)",
            (machine, missing),
        )
        conn.execute(
            "UPDATE schedules SET not_live_since = statement_timestamp() "
            "WHERE machine = %s AND id = ANY(%s) AND not_live_since IS NULL",
            (machine, missing),
        )
        alerts = query_all(
            conn,
            _StallRow,
            "UPDATE schedules SET stall_alerted_at = statement_timestamp() "
            "WHERE machine = %s AND id = ANY(%s) AND stall_alerted_at IS NULL "
            "AND not_live_since < statement_timestamp() - make_interval(secs => %s) "
            "RETURNING id, not_live_since, statement_timestamp() AS observed_at",
            (machine, missing, STALL_ALERT_AFTER_S),
        )
    for alert in alerts:
        _emit_stall(home, status_by_id[alert.id], alert)


def _emit_stall(home: Path, status: str, alert: _StallRow) -> None:
    beat = read_beat(home, alert.id)
    session_missing_s = _seconds_between(alert.not_live_since, alert.observed_at)
    silent_s = (
        session_missing_s if beat is None else _seconds_between(beat.written_at, alert.observed_at)
    )
    attributes: dict[str, object] = {
        "schedule_id": alert.id,
        "status": status,
        "silent_seconds": silent_s,
        "session_missing_seconds": session_missing_s,
    }
    if beat is None:
        _log.warning(
            "schedule %s has had no live session for %.1fs (status=%s, no round beat)",
            alert.id,
            session_missing_s,
            status,
        )
    else:
        written_at = beat.written_at.isoformat(timespec="seconds")
        attributes["beat_written_at"] = written_at
        _log.warning(
            "schedule %s has had no live session for %.1fs and no round since %s "
            "(%.1fs, status=%s)",
            alert.id,
            session_missing_s,
            written_at,
            silent_s,
            status,
        )
    telemetry.emit(
        "telemetry", event_name="schedule_stalled", level="warning", attributes=attributes
    )


# meiva:note
# The two instants can come from different clocks -- a file written by the schedule and a
# statement run by Postgres -- so a small negative is clock skew, not a round in the future.
def _seconds_between(earlier: datetime, later: datetime) -> float:
    return round(max((later - earlier).total_seconds(), 0.0), 3)


@dataclass(frozen=True)
class _ClockRow:
    now: datetime


def _database_clock(pool: ConnectionPool) -> datetime:
    with pool.connection() as conn:
        row = query_one(conn, _ClockRow, "SELECT clock_timestamp() AS now")
    if row is None:
        raise RuntimeError("clock_timestamp() returned no row")
    return row.now

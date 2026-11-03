# meiva:note
# The round-deadline alert for resident schedules: the one liveness fact a live session
# cannot give.
#
# A resident schedule keeps its session while it wedges inside a round, so the sessionless
# stall alert never fires for it. Its own heartbeat does say when it expected to be back;
# this module reads that promise against the database clock and reports the one it stopped
# keeping, once per episode, and re-arms when a round lands again.
#
# The clock comes from Postgres rather than this process: the manager runs under the strict
# profile and may not read a clock itself, and the database is the same wall clock the file
# mtimes follow.

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from psycopg_pool import ConnectionPool

from base import telemetry
from base.daemon.schedules.heartbeat import (
    Beat,
    is_overdue,
    read_alert_marks,
    read_beat,
    write_alert_marks,
)
from base.deploy.schedule_timing import SCHEDULE_PROGRESS_SKEW_S

__all__ = ["report_late_rounds"]

_log = logging.getLogger(__name__)


def report_late_rounds(
    pool: ConnectionPool[Any],
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


def _database_clock(pool: ConnectionPool[Any]) -> datetime:
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT clock_timestamp()")
        row = cur.fetchone()
    if row is None:
        raise RuntimeError("clock_timestamp() returned no row")
    return row[0]

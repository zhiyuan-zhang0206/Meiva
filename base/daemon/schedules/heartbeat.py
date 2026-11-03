# meiva:note
# Round heartbeats for resident schedules.
#
# A resident schedule runs one long-lived loop (`while True` plus its own `next_fire`)
# instead of being launched per cron slot. Nothing about it reaches `schedule_fire_log`,
# and `launch_count` stays at 1 for its whole life, so a loop that stopped iterating
# looks exactly like one with nothing to do.
#
# The heartbeat closes that gap from the inside: each round writes down the instant by
# which the next round must have started (`alive_by`). The writer knows its own cadence
# -- including how long one round may legitimately take -- so no reader needs a
# per-schedule configuration. The schedule manager turns a passed `alive_by` into
# `schedule_not_progressing` (services/wake/schedule_manager/round_progress.py).
#
# The file is deliberately plain JSON under a stable path so that a reader outside the
# manager, including an OS-level check, can read it without this module.

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TypeGuard

__all__ = [
    "Beat",
    "alert_marks_path",
    "beat_path",
    "is_overdue",
    "read_alert_marks",
    "read_beat",
    "write_alert_marks",
    "write_beat",
]


# meiva:note
# A parsed heartbeat is a value, not a dict: the file is a boundary, so it is validated
# once here and every reader afterwards works with typed fields instead of guessing
# which keys a writer happened to include.
@dataclass(frozen=True)
class Beat:
    schedule_id: int
    written_at: datetime
    alive_by: datetime

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> Beat | None:
        schedule_id = payload.get("schedule_id")
        written_at = payload.get("written_at")
        alive_by = payload.get("alive_by")
        if not isinstance(schedule_id, int) or isinstance(schedule_id, bool):
            return None
        if not isinstance(written_at, str) or not isinstance(alive_by, str):
            return None
        try:
            return cls(schedule_id, _parse_instant(written_at), _parse_instant(alive_by))
        except ValueError:
            return None


def beat_path(home: Path, schedule_id: int) -> Path:
    return home / "state" / "schedules" / f"{schedule_id}.heartbeat.json"


def alert_marks_path(home: Path) -> Path:
    return home / "state" / "schedules" / "progress-alerts.json"


# meiva:note
# Written after a round finishes and before the loop sleeps, so the deadline that lands
# on disk covers the gap the loop is about to take. `alive_by` is absolute, which keeps
# the comparison immune to how long the writer took to reach us.
def write_beat(
    home: Path,
    schedule_id: int,
    *,
    written_at: datetime,
    alive_by: datetime,
    budget_s: float | None = None,
) -> Path:
    path = beat_path(home, schedule_id)
    payload: dict[str, object] = {
        "schedule_id": schedule_id,
        "written_at": _as_utc(written_at).isoformat(timespec="seconds"),
        "alive_by": _as_utc(alive_by).isoformat(timespec="seconds"),
    }
    # meiva:note
    # The manager reads the promise, not this number: it is written for the person who later asks
    # why a dead loop went unnamed for forty minutes, which is the one way a self-calibrating
    # promise can decay -- a single slow round widens every promise after it. Recording the term
    # in the file makes that answer one `cat` away instead of a log hunt. Readers that predate this
    # key ignore it; `Beat` still validates only the three it needs.
    if budget_s is not None:
        payload["round_budget_s"] = round(budget_s, 3)
    _write_atomically(path, json.dumps(payload, sort_keys=True) + "\n")
    return path


def read_beat(home: Path, schedule_id: int) -> Beat | None:
    payload = _read_json(beat_path(home, schedule_id))
    return None if payload is None else Beat.from_payload(payload)


# meiva:note
# `skew_s` absorbs the manager's reconcile poll and a late wake-up; it is not meant to
# absorb a round. A writer that needs a whole round of slack says so in `alive_by`,
# where it knows the number instead of guessing it here.
def is_overdue(beat: Beat | None, *, now: datetime, skew_s: float) -> bool:
    if beat is None:
        return False
    return _as_utc(now) >= beat.alive_by + timedelta(seconds=skew_s)


def read_alert_marks(home: Path) -> dict[str, str]:
    payload = _read_json(alert_marks_path(home))
    if payload is None:
        return {}
    marks: dict[str, str] = {}
    for key, value in payload.items():
        if isinstance(key, str) and isinstance(value, str):
            marks[key] = value
    return marks


def write_alert_marks(home: Path, marks: dict[str, str]) -> None:
    _write_atomically(alert_marks_path(home), json.dumps(marks, sort_keys=True) + "\n")


# meiva:note
# `isinstance(value, dict)` alone narrows to `dict[Unknown, Unknown]`, which makes every
# key and value downstream unknown to the type checker. This guard states the shape the
# boundary accepts, so readers work with `object` values they can narrow one at a time.
def _is_object_dict(value: object) -> TypeGuard[dict[str, object]]:
    return isinstance(value, dict)


def _read_json(path: Path) -> dict[str, object] | None:
    try:
        payload: object = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return payload if _is_object_dict(payload) else None


def _write_atomically(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    tmp.replace(path)


def _parse_instant(raw: str) -> datetime:
    return _as_utc(datetime.fromisoformat(raw))


def _as_utc(moment: datetime) -> datetime:
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)

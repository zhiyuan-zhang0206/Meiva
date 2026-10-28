# meiva:note
# What a detector writes down before it tries to tell anyone.
#
# Every "detect, then notify" path carries one assumption it never states: that the notifying
# works. On 2026-10-28 a burn-rate gate crossed its line, called `send_message`, got
# `GatewayUnavailable: Connection refused`, and left one log line behind -- nobody learned until
# the next night. The detection was right; the delivery was the whole of the evidence.
#
# This module makes the record the first effect and the send the second: `record()` appends one
# line to `$MEIVA_HOME/state/alarms/<date>.jsonl` before any transport is opened,
# `mark_delivered()` appends the transport's answer onto the same record, and `open_alarms()` is
# the reader a later wake reports from. One path, one format, one reader: every guard on a
# machine writes here, so any periodic job can answer "what fired while nobody was listening"
# without knowing which guard fired it.
# meiva:link base/events/alarms/docs/alarms.meiva.okf.md

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path

__all__ = [
    "Alarm",
    "acknowledge",
    "mark_delivered",
    "mark_notified",
    "open_alarms",
    "record",
]

# meiva:note
# The home-relative directory every machine's guards share. Records are dated files
# so a day's alarms are one bounded read and an operator can delete a day without
# rewriting anything.
_ALARM_DIR = "state/alarms"

# meiva:note
# How many dated files the reader folds. Two days covers a guard that fires late on
# one day and is read on the next; anything older is history the operator reads by
# hand, not a wake-up.
_KEPT_DAYS = 2

# meiva:note
# Free text is bounded so one alarm cannot grow a line without limit: the reading
# belongs in the record, the detail belongs in the log the alarm points at.
_TEXT_CHARS = 500


# meiva:note
# One detection, as the guards and the reader see it: what fired, on what, the judgement and the
# reading behind it, who owns it, and how far it got.
# meiva:link base/events/alarms/docs/alarms.meiva.okf.md
@dataclass(frozen=True)
class Alarm:
    id: str
    at: datetime
    kind: str
    subject: str
    judgement: str
    reading: str
    owner: str
    source: str
    delivered: bool = False
    delivery_error: str | None = None
    notified_at: datetime | None = None
    acked_at: datetime | None = None

    # meiva:note
    # One line for whoever reads the alarm: what fired, on what, and what for.
    # meiva:link base/events/alarms/docs/alarms.meiva.okf.md
    def line(self) -> str:
        told = "delivered" if self.delivered else f"NOT delivered: {self.delivery_error}"
        return (
            f"{self.at:%m-%d %H:%M} {self.kind} on {self.subject} — {self.judgement} "
            f"[{self.reading}] owner={self.owner} ({told})"
        )


# meiva:note
# The day file one record belongs in, named for the day the detection happened on
# the writing machine: an operator reads `ls state/alarms` as a calendar.
def _day_path(home: Path, at: datetime) -> Path:
    return home / _ALARM_DIR / f"{at:%Y-%m-%d}.jsonl"


# meiva:note
# Append one line and push it through to the disk before returning. The fsync is the
# point of this module: a record that only reached the page cache dies with the
# machine that was already in trouble.
def _append(home: Path, at: datetime, payload: Mapping[str, object]) -> None:
    path = _day_path(home, at)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(dict(payload), sort_keys=True, ensure_ascii=True) + "\n"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line)
        handle.flush()
        os.fsync(handle.fileno())


# meiva:note
# The record's id is derived from its own content, not drawn at random: two rounds that raise the
# same alarm in the same microsecond are the same alarm, and the same line written twice folds into
# one record instead of two. Nothing here reads the machine it runs on.
def _alarm_id(*, at: datetime, kind: str, subject: str, owner: str, source: str) -> str:
    material = "|".join((at.isoformat(), kind, subject, owner, source))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:12]


# meiva:note
# Write the detection down and return it. Nothing here opens a socket: a caller can
# reach this function on a machine whose gateway, database and network are all gone.
def record(
    home: Path,
    *,
    kind: str,
    subject: str,
    judgement: str,
    reading: str,
    owner: str,
    source: str,
    now: datetime,
) -> Alarm:
    alarm = Alarm(
        id=_alarm_id(at=now, kind=kind, subject=subject, owner=owner, source=source),
        at=now,
        kind=kind[:_TEXT_CHARS],
        subject=subject[:_TEXT_CHARS],
        judgement=judgement[:_TEXT_CHARS],
        reading=reading[:_TEXT_CHARS],
        owner=owner[:_TEXT_CHARS],
        source=source[:_TEXT_CHARS],
    )
    _append(
        home,
        now,
        {
            "event": "raised",
            "id": alarm.id,
            "at": alarm.at.isoformat(),
            "kind": alarm.kind,
            "subject": alarm.subject,
            "judgement": alarm.judgement,
            "reading": alarm.reading,
            "owner": alarm.owner,
            "source": alarm.source,
        },
    )
    return alarm


# meiva:note
# The transport outcome, on the record it belongs to. `error=None` is a delivery;
# anything else is the reason the operator will read instead of guessing.
def mark_delivered(home: Path, alarm_id: str, *, error: str | None, now: datetime) -> None:
    _append(home, now, {"event": "delivery", "id": alarm_id, "at": now.isoformat(), "error": error})


# meiva:note
# What a reader reported, so the next wake does not report it again. Reporting is not
# acknowledging: the record stays open until `acknowledge()`.
def mark_notified(home: Path, alarm_ids: Iterable[str], *, now: datetime) -> None:
    for alarm_id in alarm_ids:
        _append(home, now, {"event": "notified", "id": alarm_id, "at": now.isoformat()})


# meiva:note
# Close one alarm: whoever owns it has seen it and is acting.
# meiva:link base/events/alarms/docs/alarms.meiva.okf.md
def acknowledge(home: Path, alarm_id: str, *, now: datetime) -> None:
    _append(home, now, {"event": "acked", "id": alarm_id, "at": now.isoformat()})


# meiva:note
# Fold the day files into one alarm per id, latest event winning. The days are walked
# oldest first because an event lands in the file of the day it happened on: a delivery
# five minutes after midnight belongs to a later file than the alarm it closes. A
# malformed line is skipped rather than fatal: a half-written line from a killed process
# must not hide the alarms around it.
def _fold(home: Path, *, now: datetime) -> dict[str, Alarm]:
    seen: dict[str, Alarm] = {}
    for days_ago in range(_KEPT_DAYS - 1, -1, -1):
        day = now - timedelta(days=days_ago)
        path = _day_path(home, day)
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for raw in text.splitlines():
            try:
                row = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(row, dict):
                continue
            seen = _apply(seen, row)
    return seen


# meiva:note
# One line of the file onto the folded state. Events arrive in file order, so the
# last write for a field wins; an event for an unknown id (its `raised` line fell out
# of the read window) is dropped rather than guessed at.
def _apply(seen: dict[str, Alarm], row: Mapping[str, object]) -> dict[str, Alarm]:
    alarm_id = row.get("id")
    event = row.get("event")
    if not isinstance(alarm_id, str) or not isinstance(event, str):
        return seen
    if event == "raised":
        at = _moment(row.get("at"))
        if at is None:
            return seen
        seen[alarm_id] = Alarm(
            id=alarm_id,
            at=at,
            kind=str(row.get("kind", "")),
            subject=str(row.get("subject", "")),
            judgement=str(row.get("judgement", "")),
            reading=str(row.get("reading", "")),
            owner=str(row.get("owner", "")),
            source=str(row.get("source", "")),
        )
        return seen
    current = seen.get(alarm_id)
    if current is None:
        return seen
    at = _moment(row.get("at")) or current.at
    if event == "delivery":
        error = row.get("error")
        seen[alarm_id] = replace(
            current, delivered=error is None, delivery_error=None if error is None else str(error)
        )
    elif event == "notified":
        seen[alarm_id] = replace(current, notified_at=at)
    elif event == "acked":
        seen[alarm_id] = replace(current, acked_at=at)
    return seen


def _moment(raw: object) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


# meiva:note
# The alarms whose transport never took them and nobody closed, once they are older than
# `older_than`. Two filters earn their place: the age guard keeps a caller from reporting
# an alarm whose own sender is still inside the request that raised it, and `delivered`
# keeps a message the transport accepted off the list -- re-reporting every successful
# send would turn the reader into an echo. `limit` bounds one wake-up's report.
def open_alarms(
    home: Path,
    *,
    older_than: timedelta,
    now: datetime,
    limit: int = 20,
) -> list[Alarm]:
    folded = _fold(home, now=now)
    due = [
        alarm
        for alarm in folded.values()
        if not alarm.delivered
        and alarm.notified_at is None
        and alarm.acked_at is None
        and now - alarm.at >= older_than
    ]
    due.sort(key=lambda alarm: alarm.at)
    return due[:limit]

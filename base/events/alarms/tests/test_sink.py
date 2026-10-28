from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from base.events.alarms import (
    acknowledge,
    mark_delivered,
    mark_notified,
    open_alarms,
    record,
)

# meiva:note
# The moment every case below starts from: a detector that crossed its line at
# 15:53 and has to decide what to leave behind.
# meiva:link base/events/alarms/docs/alarms.meiva.okf.md
_NOW = datetime(2026, 10, 29, 7, 53, tzinfo=UTC)


# meiva:note
# One detector writing one alarm, with the identity of the guard that fired, so a
# reader can tell whose judgement it is acting on.
# meiva:link base/events/alarms/docs/alarms.meiva.okf.md
def _raise(home: Path, *, now: datetime = _NOW, kind: str = "burn-rate") -> str:
    alarm = record(
        home,
        kind=kind,
        subject="deepseek wallet",
        judgement="spend above three times the trailing rate",
        reading="2.9h, 37.5 CNY",
        owner="#1818",
        source="schedules/jobs/agent_watch.py",
        now=now,
    )
    return alarm.id


def _day_file(home: Path, now: datetime = _NOW) -> Path:
    return home / "state" / "alarms" / f"{now:%Y-%m-%d}.jsonl"


def _rows(home: Path, now: datetime = _NOW) -> list[dict[str, object]]:
    text = _day_file(home, now).read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


# meiva:note
# The record is the first effect: by the time `record` returns, the detection is on
# the disk of the machine that detected it, before any transport is opened. This is
# the half of the job a refused connection cannot take away.
# meiva:link base/events/alarms/docs/alarms.meiva.okf.md
def test_a_raised_alarm_is_on_disk_before_any_send(tmp_path: Path) -> None:
    alarm_id = _raise(tmp_path)

    rows = _rows(tmp_path)

    assert [row["event"] for row in rows] == ["raised"]
    assert rows[0]["id"] == alarm_id
    assert rows[0]["kind"] == "burn-rate"
    assert rows[0]["owner"] == "#1818"


# meiva:note
# A refused transport does not close the alarm: the delivery event carries the reason,
# and the reader still sees the record as unanswered evidence. On 2026-10-28 the send
# raised `Connection refused` and the only remain was one log line.
# meiva:link base/events/alarms/docs/alarms.meiva.okf.md
def test_a_failed_send_leaves_the_alarm_readable_with_its_reason(tmp_path: Path) -> None:
    alarm_id = _raise(tmp_path)
    mark_delivered(
        tmp_path,
        alarm_id,
        error="GatewayUnavailable: [Errno 61] Connection refused",
        now=_NOW + timedelta(seconds=2),
    )

    due = open_alarms(tmp_path, older_than=timedelta(minutes=15), now=_NOW + timedelta(minutes=20))

    assert [alarm.id for alarm in due] == [alarm_id]
    assert due[0].delivered is False
    assert due[0].delivery_error == "GatewayUnavailable: [Errno 61] Connection refused"
    assert due[0].notified_at is None


# meiva:note
# Reporting is the reader's act, and it closes the alarm against the next wake without
# erasing it: the raised line stays on disk as the record of what fired.
# meiva:link base/events/alarms/docs/alarms.meiva.okf.md
def test_a_reported_alarm_is_not_reported_again(tmp_path: Path) -> None:
    alarm_id = _raise(tmp_path)
    later = _NOW + timedelta(minutes=30)
    mark_notified(tmp_path, [alarm_id], now=later)

    assert open_alarms(tmp_path, older_than=timedelta(minutes=15), now=later) == []
    assert [row["event"] for row in _rows(tmp_path)] == ["raised", "notified"]


# meiva:note
# Whoever owns the alarm closes it out; an acknowledged alarm is settled evidence, not
# pending work, so no later reader reports it again.
# meiva:link base/events/alarms/docs/alarms.meiva.okf.md
def test_an_acknowledged_alarm_is_closed(tmp_path: Path) -> None:
    alarm_id = _raise(tmp_path)
    later = _NOW + timedelta(hours=2)
    acknowledge(tmp_path, alarm_id, now=later)

    assert open_alarms(tmp_path, older_than=timedelta(minutes=15), now=later) == []


# meiva:note
# A line half-written by a process that died mid-append must not hide the alarms around
# it: the reader skips what it cannot parse and keeps folding the file.
# meiva:link base/events/alarms/docs/alarms.meiva.okf.md
def test_a_malformed_line_does_not_hide_its_neighbours(tmp_path: Path) -> None:
    first = _raise(tmp_path, kind="burn-rate")
    with (_day_file(tmp_path)).open("a", encoding="utf-8") as handle:
        handle.write('{"event": "raised", "id": "trunc')
        handle.write("\n")
    second = _raise(tmp_path, kind="disk")

    due = open_alarms(tmp_path, older_than=timedelta(minutes=15), now=_NOW + timedelta(minutes=20))

    assert [alarm.id for alarm in due] == [first, second]


# meiva:note
# The age guard belongs to the sender, not the reader: an alarm younger than the window
# is still owned by the call that raised it, and a concurrent reader reports nothing.
# meiva:link base/events/alarms/docs/alarms.meiva.okf.md
def test_an_alarm_younger_than_the_age_guard_is_left_to_its_sender(tmp_path: Path) -> None:
    _raise(tmp_path)

    due = open_alarms(tmp_path, older_than=timedelta(minutes=15), now=_NOW + timedelta(minutes=5))

    assert due == []


# meiva:note
# A guard that fires at 23:59 is read by a wake at 00:05, so the fold crosses the day
# boundary: the reader looks at the dated files, not at "today".
# meiva:link base/events/alarms/docs/alarms.meiva.okf.md
def test_an_alarm_raised_late_on_one_day_is_read_on_the_next(tmp_path: Path) -> None:
    raised_at = datetime(2026, 10, 28, 23, 59, tzinfo=UTC)
    alarm_id = _raise(tmp_path, now=raised_at)
    read_at = datetime(2026, 10, 29, 0, 5, tzinfo=UTC)

    due = open_alarms(tmp_path, older_than=timedelta(minutes=1), now=read_at)

    assert [alarm.id for alarm in due] == [alarm_id]
    assert _day_file(tmp_path, raised_at).exists()


# meiva:note
# An event whose `raised` line fell out of the read window is dropped, not guessed at:
# the reader never invents an alarm it cannot show the operator in full.
# meiva:link base/events/alarms/docs/alarms.meiva.okf.md
def test_an_event_for_an_unknown_alarm_is_dropped(tmp_path: Path) -> None:
    mark_delivered(tmp_path, "0123456789ab", error=None, now=_NOW)

    due = open_alarms(tmp_path, older_than=timedelta(minutes=15), now=_NOW + timedelta(minutes=20))

    assert due == []


# meiva:note
# A message the transport accepted is not owed to anyone: re-reporting every successful send
# would echo the watch back at its own lead, so `delivered` closes the alarm against the reader.
# meiva:link base/events/alarms/docs/alarms.meiva.okf.md
def test_a_delivered_alarm_is_not_reported_again(tmp_path: Path) -> None:
    alarm_id = _raise(tmp_path)
    mark_delivered(tmp_path, alarm_id, error=None, now=_NOW + timedelta(seconds=1))

    later = _NOW + timedelta(minutes=30)
    assert open_alarms(tmp_path, older_than=timedelta(minutes=15), now=later) == []
    assert [row["event"] for row in _rows(tmp_path)] == ["raised", "delivery"]


# meiva:note
# Every event lands in the file of the day it happened on, so a delivery a few minutes after
# midnight lives one file later than the alarm it closes. The fold walks the days oldest first;
# read the other way, the raised line would win and the alarm would come back from the dead.
# meiva:link base/events/alarms/docs/alarms.meiva.okf.md
def test_a_delivery_after_midnight_closes_an_alarm_raised_before_it(tmp_path: Path) -> None:
    raised_at = datetime(2026, 10, 28, 23, 59, tzinfo=UTC)
    alarm_id = _raise(tmp_path, now=raised_at)
    delivered_at = datetime(2026, 10, 29, 0, 1, tzinfo=UTC)
    mark_delivered(
        tmp_path, alarm_id, error="GatewayUnavailable: connection refused", now=delivered_at
    )

    read_at = datetime(2026, 10, 29, 0, 30, tzinfo=UTC)
    due = open_alarms(tmp_path, older_than=timedelta(minutes=15), now=read_at)

    assert [alarm.id for alarm in due] == [alarm_id]
    assert due[0].delivery_error == "GatewayUnavailable: connection refused"

    mark_notified(tmp_path, [alarm_id], now=read_at)
    again = open_alarms(
        tmp_path, older_than=timedelta(minutes=15), now=read_at + timedelta(hours=1)
    )
    assert again == []

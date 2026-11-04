# meiva:note
# The heartbeat is the only thing that distinguishes "the loop has nothing to do"
# from "the loop stopped", so these cases pin the file's shape (plain JSON at a
# stable path, readable outside the manager) and the deadline comparison --
# including the malformed file, which must read as "no heartbeat" rather than as
# "late", because a broken file is not evidence that the loop ran.

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from base.daemon.schedules.heartbeat import (
    alert_marks_path,
    beat_path,
    is_overdue,
    read_alert_marks,
    read_beat,
    write_alert_marks,
    write_beat,
    write_round_beat,
)
from base.host.boundary.json_values import is_object_dict
from base.host.boundary.untyped import object_of

NOW = datetime(2026, 11, 3, 18, 0, tzinfo=UTC)


# meiva:note
# The heartbeat file as the plain JSON object an outside reader sees.
def _loaded(path: Path) -> dict[str, object]:
    loaded = object_of(json.loads, path.read_text())
    assert is_object_dict(loaded)
    return loaded


def test_a_round_lands_as_plain_json_under_a_stable_path(tmp_path: Path) -> None:
    path = write_beat(tmp_path, 42, written_at=NOW, alive_by=NOW + timedelta(hours=1))

    assert path == beat_path(tmp_path, 42) == tmp_path / "state/schedules/42.heartbeat.json"
    loaded = _loaded(path)
    assert loaded["schedule_id"] == 42
    alive_by = loaded["alive_by"]
    written_at = loaded["written_at"]
    assert isinstance(alive_by, str) and alive_by.startswith("2026-11-03T19:00")
    assert isinstance(written_at, str) and written_at.startswith("20")


def test_a_deadline_passes_only_after_its_own_instant_plus_the_skew(tmp_path: Path) -> None:
    write_beat(tmp_path, 7, written_at=NOW, alive_by=NOW)
    beat = read_beat(tmp_path, 7)

    assert is_overdue(beat, now=NOW + timedelta(seconds=59), skew_s=60) is False
    assert is_overdue(beat, now=NOW + timedelta(seconds=60), skew_s=60) is True


def test_a_missing_or_broken_heartbeat_is_not_late(tmp_path: Path) -> None:
    assert read_beat(tmp_path, 9) is None
    assert is_overdue(None, now=NOW, skew_s=0) is False

    path = beat_path(tmp_path, 9)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json")
    assert read_beat(tmp_path, 9) is None
    assert is_overdue(read_beat(tmp_path, 9), now=NOW, skew_s=0) is False

    path.write_text(json.dumps({"schedule_id": 9}))
    assert read_beat(tmp_path, 9) is None


def test_alert_marks_round_trip_and_land_at_a_documented_path(tmp_path: Path) -> None:
    assert read_alert_marks(tmp_path) == {}
    write_alert_marks(tmp_path, {"42": "2026-11-03T19:00:00+00:00"})

    assert alert_marks_path(tmp_path) == tmp_path / "state/schedules/progress-alerts.json"
    assert read_alert_marks(tmp_path) == {"42": "2026-11-03T19:00:00+00:00"}


# meiva:note
# The budget is written for the question "why did this alert come late": the promise only widens,
# so one slow round raises every later deadline, and the number that did it must be readable
# without the loop's logs. It is not part of `Beat`, so a reader that predates it is unaffected.
def test_the_measured_budget_rides_along_without_changing_the_contract(tmp_path: Path) -> None:
    path = write_beat(
        tmp_path, 55, written_at=NOW, alive_by=NOW + timedelta(hours=1), budget_s=640.5
    )
    loaded = _loaded(path)

    assert loaded["round_budget_s"] == 640.5
    beat = read_beat(tmp_path, 55)
    assert beat is not None and beat.alive_by == NOW + timedelta(hours=1)

    plain = write_beat(tmp_path, 56, written_at=NOW, alive_by=NOW)
    assert "round_budget_s" not in json.loads(plain.read_text())


# meiva:note
# A round beat may be absent, and its absence is silent by design: a stored schedule script written
# before the parameters existed keeps running exactly as it did, and a beat that cannot be written
# must never end the loop it reports on.
def test_a_round_beat_without_a_home_or_an_id_writes_nothing(tmp_path: Path) -> None:
    write_round_beat(None, 42, now=NOW, sleep_s=60, budget_s=5)
    write_round_beat(tmp_path, None, now=NOW, sleep_s=60, budget_s=5)

    assert list(tmp_path.rglob("*.heartbeat.json")) == []


def test_a_round_beat_promises_the_sleep_plus_the_measured_round(tmp_path: Path) -> None:
    write_round_beat(tmp_path, 39, now=NOW, sleep_s=60, budget_s=12.5)

    beat = read_beat(tmp_path, 39)
    assert beat is not None
    assert beat.written_at == NOW
    # meiva:note
    # The file keeps whole seconds, so the promise is compared at that precision and the measured
    # budget keeps its fraction -- the question it answers ("which round widened this?") needs it.
    assert beat.alive_by == NOW + timedelta(seconds=72)
    assert json.loads(beat_path(tmp_path, 39).read_text())["round_budget_s"] == 12.5


def test_a_round_beat_that_cannot_be_written_does_not_raise(tmp_path: Path) -> None:
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    (blocked / "state").write_text("")

    write_round_beat(blocked, 39, now=NOW, sleep_s=60, budget_s=1)

    assert (blocked / "state").is_file()

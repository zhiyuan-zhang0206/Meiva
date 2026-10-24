from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from cli.commands.lifecycle import stand_down
from cli.commands.lifecycle.start.stand_down_gate import stand_down_step

NOW = datetime(2026, 10, 21, 5, 0, tzinfo=UTC)


def _home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    (home / "run" / "meiva-root").mkdir(parents=True)
    return home


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [(None, None), (0, True), (45, True)],
)
def test_window_until_carries_the_declared_maintenance_window(
    seconds: int | None, expected: bool | None
) -> None:
    until = stand_down.window_until(seconds, now=NOW)

    assert until is None if expected is None else until is not None
    if seconds:
        assert until == NOW + timedelta(seconds=seconds)


class TestRecord:
    def test_a_record_round_trips_with_its_reason_and_deadline(self, tmp_path: Path) -> None:
        home = _home(tmp_path)
        until = NOW + timedelta(hours=2)

        stand_down.record(home, now=NOW, until=until, reason="maintenance", machine="mba")

        held = stand_down.read(home)
        assert held is not None
        assert (held.since, held.until, held.reason, held.machine) == (
            NOW,
            until,
            "maintenance",
            "mba",
        )
        assert (home / "run" / "meiva-root" / "stand-down.json").exists()

    def test_a_stop_without_a_window_waits_for_an_explicit_start(self, tmp_path: Path) -> None:
        home = _home(tmp_path)

        stand_down.record(home, now=NOW, until=None, reason=None)

        held = stand_down.read(home)
        assert held is not None
        assert held.until is None
        assert "until an explicit start" in held.describe(NOW)

    def test_an_unreadable_record_reads_as_stopped_rather_than_as_freedom(
        self, tmp_path: Path
    ) -> None:
        home = _home(tmp_path)
        (home / "run" / "meiva-root" / "stand-down.json").write_text("{not json")

        held = stand_down.read(home)
        assert held is not None and held.until is None and held.since is None
        step = stand_down_step(
            boot_attempt=True,
            recorded=True,
            deadline_passed=held.deadline_passed(NOW),
            window=held.describe(NOW),
        )
        assert step.skip, "the boot job leaves the home down"
        assert not step.clear


class TestDescribe:
    def test_a_window_still_open_says_how_long_it_has_left(self) -> None:
        held = stand_down.StandDown(
            since=NOW, until=NOW + timedelta(seconds=90), reason=None, machine=None
        )

        assert held.describe(NOW) == "declared window ends in 90s"
        assert not held.deadline_passed(NOW)

    def test_a_window_that_ended_says_how_long_ago(self) -> None:
        held = stand_down.StandDown(
            since=NOW, until=NOW - timedelta(seconds=7), reason=None, machine=None
        )

        assert held.describe(NOW) == "declared window ended 7s ago"
        assert held.deadline_passed(NOW)

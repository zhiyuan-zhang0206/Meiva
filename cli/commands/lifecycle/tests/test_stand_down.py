from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from cli.commands.lifecycle import stand_down


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
    now = datetime(2026, 10, 21, 5, 0, tzinfo=UTC)

    until = stand_down.window_until(seconds, now=now)

    assert until is None if expected is None else until is not None
    if seconds:
        assert until == now + timedelta(seconds=seconds)


class TestRecord:
    def test_a_record_round_trips_with_its_reason_and_deadline(self, tmp_path: Path) -> None:
        home = _home(tmp_path)
        until = datetime.now(UTC) + timedelta(hours=2)

        stand_down.record(home, until=until, reason="maintenance", machine="mba")

        held = stand_down.read(home)
        assert held is not None
        assert (held.until, held.reason, held.machine) == (until, "maintenance", "mba")
        assert (home / "run" / "meiva-root" / "stand-down.json").exists()

    def test_a_stop_without_a_window_waits_for_an_explicit_start(self, tmp_path: Path) -> None:
        home = _home(tmp_path)

        stand_down.record(home, until=None, reason=None)

        held = stand_down.read(home)
        assert held is not None
        assert held.until is None
        assert "until an explicit start" in held.describe()

    def test_an_unreadable_record_reads_as_stopped_rather_than_as_freedom(
        self, tmp_path: Path
    ) -> None:
        home = _home(tmp_path)
        (home / "run" / "meiva-root" / "stand-down.json").write_text("{not json")

        held = stand_down.read(home)
        assert held is not None and held.until is None
        assert stand_down.blocks_boot(home) is not None


class TestGate:
    def test_the_boot_job_leaves_a_stopped_home_alone(self, tmp_path: Path) -> None:
        home = _home(tmp_path)
        stand_down.record(home, until=None, reason="night stop")

        line = stand_down.gate(home, boot_attempt=True)

        assert line is not None and line.startswith("stand-down")
        assert "boot job leaves this home stopped" in line
        assert stand_down.read(home) is not None, "the boot job must not clear the record"

    def test_the_boot_job_resumes_once_the_declared_window_has_passed(self, tmp_path: Path) -> None:
        home = _home(tmp_path)
        stand_down.record(home, until=datetime.now(UTC) - timedelta(seconds=1), reason=None)

        assert stand_down.gate(home, boot_attempt=True) is None
        assert stand_down.read(home) is not None, "an expired record still waits for a start"

    def test_an_explicit_start_clears_the_record_and_says_so(self, tmp_path: Path) -> None:
        home = _home(tmp_path)
        stand_down.record(home, until=None, reason=None)

        line = stand_down.gate(home, boot_attempt=False)

        assert line == "stand-down cleared by an explicit start"
        assert stand_down.read(home) is None

    def test_an_ordinary_start_on_a_running_home_says_nothing(self, tmp_path: Path) -> None:
        home = _home(tmp_path)

        assert stand_down.gate(home, boot_attempt=False) is None

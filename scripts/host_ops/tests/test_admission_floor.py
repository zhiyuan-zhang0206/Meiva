from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.host_ops import admission_floor
from scripts.host_ops import admission_run as admission
from scripts.host_ops.tests.admission_support import (
    BudgetSequence,
    Recorder,
    run_admission,
)

# meiva:link scripts/host_ops/docs/admission-run/disk-floor.meiva.okf.md


# meiva:note
# The pure half of the disk floor (task #5872): the verdict is a function of the readings and of
# the state the previous invocation left behind, never of a single sample.
class TestFloorVerdict:
    @pytest.mark.parametrize(
        ("readings", "refused_before", "expected"),
        [
            ([22.9], False, "warn"),
            ([22.9, 22.8, 22.9], False, "refuse"),
            ([22.9, 23.5, 23.5], False, "warn"),
            ([23.0, 23.0, 23.0], False, "warn"),
            ([23.5, 23.5, 23.5], True, "refuse"),
            ([24.5, 24.5, 24.5], True, "ok"),
            ([26.0], True, "ok"),
            ([22.0, 22.0, 22.0], True, "refuse"),
        ],
    )
    def test_the_verdict_is_the_readings_and_the_state(
        self, readings: list[float], refused_before: bool, expected: str
    ) -> None:
        assert (
            admission_floor.floor_verdict(
                readings,
                refuse_gib=23.0,
                warn_gib=24.0,
                reset_gib=24.5,
                refused_before=refused_before,
            )
            == expected
        )

    def test_leaving_the_refusal_below_the_warning_line_warns(self) -> None:
        assert (
            admission_floor.floor_verdict(
                [24.6], refuse_gib=23.0, warn_gib=25.0, reset_gib=24.5, refused_before=True
            )
            == "warn"
        )

    def test_a_zero_floor_is_always_ok(self) -> None:
        assert (
            admission_floor.floor_verdict(
                [0.0, 0.0, 0.0],
                refuse_gib=0.0,
                warn_gib=0.0,
                reset_gib=1.5,
                refused_before=True,
            )
            == "ok"
        )

    # meiva:note
    # A reader that stops answering ends the probe with fewer samples, and fewer samples can
    # warn but never refuse: a flaky probe must not block work.
    def test_a_short_probe_cannot_refuse(self) -> None:
        assert (
            admission_floor.floor_verdict(
                [22.0, 22.0],
                refuse_gib=23.0,
                warn_gib=24.0,
                reset_gib=24.5,
                refused_before=False,
            )
            == "warn"
        )


# meiva:note
# The disk floor (task #5630): a machine below the refuse line starts nothing, a machine
# between the two lines starts and says so, and a Linux reading (the kernel's MemAvailable)
# is not a data volume at all, so the floor does not apply to it.
class TestDiskFloor:
    def test_below_the_floor_starts_nothing_and_records_why(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code, recorder = run_admission(
            ["--peak-mb", "600", "--", "python", "eval.py", "--jobs", "8"],
            free_gib=22.0,
            tmp_path=tmp_path,
        )

        err = capsys.readouterr().err
        assert code == 3
        assert "refusing to start eval.py" in err
        assert "23.0 GiB floor" in err
        assert recorder.calls == []
        event = json.loads((tmp_path / "events.jsonl").read_text().splitlines()[0])
        assert event["kind"] == "refused"
        assert event["free_gib"] == pytest.approx(22.0)
        assert event["floor_gib"] == pytest.approx(admission_floor.DEFAULT_DISK_REFUSE_GIB)
        assert event["source"] == "disk"

    def test_between_the_lines_starts_with_a_warning(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code, recorder = run_admission(
            ["--peak-mb", "600", "--", "python", "eval.py", "--jobs", "8"],
            free_gib=23.5,
            tmp_path=tmp_path,
        )

        assert code == 0
        assert "warning eval.py" in capsys.readouterr().err
        assert recorder.calls == [["python", "eval.py", "--jobs", "8"]]
        event = json.loads((tmp_path / "events.jsonl").read_text().splitlines()[0])
        assert event["kind"] == "warned"

    # meiva:note
    # The reverse check: the same 22 GiB host runs once the floor is out of the way.
    def test_a_zero_floor_lets_the_same_host_start(self, tmp_path: Path) -> None:
        code, recorder = run_admission(
            [
                "--peak-mb",
                "600",
                "--disk-floor-gib",
                "0",
                "--disk-warn-gib",
                "0",
                "--",
                "python",
                "eval.py",
                "--jobs",
                "8",
            ],
            free_gib=22.0,
            tmp_path=tmp_path,
        )

        assert code == 0
        assert recorder.calls == [["python", "eval.py", "--jobs", "8"]]

    def test_a_memory_budget_is_not_a_data_volume(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # meiva:note
        # A Linux reading prices MemAvailable, where swap does not grow the volume.
        code, recorder = run_admission(
            ["--peak-mb", "600", "--", "python", "eval.py", "--jobs", "8"],
            free_gib=4.0,
            source="memory",
            tmp_path=tmp_path,
        )

        assert code == 0
        assert "refusing" not in capsys.readouterr().err
        assert len(recorder.calls) == 1

    def test_a_dry_run_prints_its_plan_below_the_floor(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code, recorder = run_admission(
            ["--peak-mb", "600", "--dry-run", "--", "python", "eval.py", "--jobs", "8"],
            free_gib=22.0,
            tmp_path=tmp_path,
        )

        assert code == 0
        assert "would start" in capsys.readouterr().out
        assert recorder.calls == []

    def test_three_consecutive_low_readings_refuse(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code, recorder = run_admission(
            ["--peak-mb", "600", "--", "python", "eval.py", "--jobs", "8"],
            readings=[22.4, 22.2, 22.0],
            tmp_path=tmp_path,
        )

        assert code == 3
        assert "refusing to start eval.py" in capsys.readouterr().err
        assert recorder.calls == []
        event = json.loads((tmp_path / "events.jsonl").read_text().splitlines()[0])
        assert event["kind"] == "refused"
        assert event["readings"] == 3
        assert event["lowest_gib"] == pytest.approx(22.0)
        state = json.loads((tmp_path / "disk-floor-state.json").read_text())
        assert state["refused"] is True

    # meiva:note
    # The negative control for the rule: the same three readings that refused would have let
    # the same host start under the old single-sample verdict, so this test would pass on a
    # build that ignored the second and third readings.
    def test_one_low_reading_followed_by_two_above_the_line_only_warns(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code, recorder = run_admission(
            ["--peak-mb", "600", "--", "python", "eval.py", "--jobs", "8"],
            readings=[22.4, 23.6, 23.8],
            tmp_path=tmp_path,
        )

        assert code == 0
        assert "warning eval.py" in capsys.readouterr().err
        assert recorder.calls == [["python", "eval.py", "--jobs", "8"]]
        event = json.loads((tmp_path / "events.jsonl").read_text().splitlines()[0])
        assert event["kind"] == "warned"
        assert event["readings"] == 3

    def test_a_refused_host_stays_refused_inside_the_band(self, tmp_path: Path) -> None:
        (tmp_path / "disk-floor-state.json").write_text('{"refused": true}')

        code, recorder = run_admission(
            ["--peak-mb", "600", "--", "python", "eval.py", "--jobs", "8"],
            readings=[23.6, 23.8, 24.1],
            tmp_path=tmp_path,
        )

        assert code == 3
        assert recorder.calls == []
        event = json.loads((tmp_path / "events.jsonl").read_text().splitlines()[0])
        assert event["refused_before"] is True

    def test_a_reading_at_the_reset_line_clears_the_refusal(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        (tmp_path / "disk-floor-state.json").write_text('{"refused": true}')

        code, recorder = run_admission(
            ["--peak-mb", "600", "--", "python", "eval.py", "--jobs", "8"],
            readings=[24.6, 24.7, 24.9],
            tmp_path=tmp_path,
        )

        assert code == 0
        assert "refusing" not in capsys.readouterr().err
        assert recorder.calls == [["python", "eval.py", "--jobs", "8"]]
        state = json.loads((tmp_path / "disk-floor-state.json").read_text())
        assert state["refused"] is False

    # meiva:note
    # The knob that turns the new rule off: one sample is the old verdict, so a host that
    # wants the previous behaviour can have it without a release.
    def test_one_sample_is_the_old_verdict(self, tmp_path: Path) -> None:
        code, recorder = run_admission(
            [
                "--peak-mb",
                "600",
                "--disk-samples",
                "1",
                "--",
                "python",
                "eval.py",
                "--jobs",
                "8",
            ],
            readings=[22.0],
            tmp_path=tmp_path,
        )

        assert code == 3
        assert recorder.calls == []

    def test_a_comfortable_host_costs_one_reading(self, tmp_path: Path) -> None:
        disk = BudgetSequence([26.1])

        admission.main(
            ["--peak-mb", "600", "--", "python", "eval.py", "--jobs", "8"],
            budget_reader=disk,
            pressure_reader=lambda: 1,
            psi_reader=lambda: None,
            gpu_reader=lambda: None,
            executor=Recorder(),
            home=tmp_path,
            sleeper=lambda _seconds: None,
        )

        assert disk.calls == [26.1]

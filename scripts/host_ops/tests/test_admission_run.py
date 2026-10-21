from __future__ import annotations

import json
import os
from collections.abc import Sequence
from functools import partial
from pathlib import Path

import pytest

from scripts.host_ops import admission_run as admission


def _collect(free_gib: float = 26.1, pressure: int = 1, unstable: bool = False):
    def reader() -> admission.Reading:
        return admission.Reading(
            free_gib=free_gib, source="disk", unstable=unstable, detail="df 84.0%"
        )

    return reader, lambda: pressure


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, command: Sequence[str], context: dict[str, object]) -> int:
        self.calls.append(list(command))
        return 0


# meiva:link scripts/host_ops/docs/admission-run.meiva.okf.md
class TestPlan:
    def test_the_documented_example_lands_on_two_workers(self) -> None:
        outcome = admission.plan(8, 2800, 26.1, 1, 12.0, 2.0)

        assert outcome.jobs == 2
        assert outcome.budget_gib == pytest.approx(14.1)
        assert outcome.cost_gib == pytest.approx(5.46875)
        assert outcome.overcommitted is False

    def test_a_budget_below_one_worker_still_starts_one_and_says_overcommitted(self) -> None:
        outcome = admission.plan(8, 2800, 13.0, 1, 12.0, 2.0)

        assert outcome.jobs == 1
        assert outcome.overcommitted is True

    def test_a_job_that_fits_keeps_its_requested_concurrency(self) -> None:
        outcome = admission.plan(8, 600, 26.0, 1, 12.0, 2.0)

        assert outcome.jobs == 8
        assert outcome.overcommitted is False

    def test_pressure_warn_halves_the_budget(self) -> None:
        calm = admission.plan(8, 2800, 26.1, 1, 12.0, 2.0)
        warned = admission.plan(8, 2800, 26.1, 2, 12.0, 2.0)

        assert warned.budget_gib == pytest.approx(calm.budget_gib / 2)
        assert warned.jobs == 1

    def test_pressure_critical_starts_one(self) -> None:
        outcome = admission.plan(8, 600, 200.0, 4, 12.0, 2.0)

        assert outcome.jobs == 1
        assert outcome.overcommitted is True


class TestCommandReading:
    def test_the_job_key_is_the_script_not_the_interpreter(self) -> None:
        runner = "scripts/host_ops/admission_run.py"

        assert admission.job_key([".venv/bin/python", runner, "--jobs", "8"]) == "admission_run.py"
        assert admission.job_key(["/usr/bin/uv", "run", "python", runner]) == "admission_run.py"
        assert admission.job_key(["/usr/bin/python3", "-m", "scripts.host_ops.admission_run"]) == (
            "scripts.host_ops.admission_run"
        )

    @pytest.mark.parametrize(
        ("argv", "expected"),
        [
            (["python", "x.py", "--jobs", "8"], (8, 3)),
            (["python", "x.py", "--jobs=8"], (8, 2)),
            (["python", "x.py", "-j8"], (8, 2)),
            (["python", "x.py", "-j", "8"], (8, 3)),
            (["python", "x.py", "--eval-jobs", "6"], (6, 3)),
            (["python", "x.py", "--eval-jobs=6"], (6, 2)),
            (["python", "x.py"], (1, None)),
            (["python", "x.py", "--jobs-many"], (1, None)),
        ],
    )
    def test_concurrency_is_read_from_the_command(
        self, argv: list[str], expected: tuple[int, int | None]
    ) -> None:
        assert admission.requested_jobs(argv) == expected


def _run(
    argv: list[str],
    *,
    free_gib: float = 26.1,
    pressure: int = 1,
    unstable: bool = False,
    tmp_path: Path,
) -> tuple[int, _Recorder]:
    recorder = _Recorder()
    disk, pressure_reader = _collect(free_gib, pressure, unstable)
    code = admission.main(
        argv,
        budget_reader=disk,
        pressure_reader=pressure_reader,
        psi_reader=lambda: None,
        gpu_reader=lambda: None,
        executor=recorder,
        home=tmp_path,
    )
    return code, recorder


class TestStart:
    def test_a_lowered_start_prints_one_line_and_rewrites_the_flag(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code, recorder = _run(
            ["--peak-mb", "2800", "--", "python", "eval.py", "--jobs", "8"], tmp_path=tmp_path
        )

        out = capsys.readouterr().out
        assert code == 0
        assert out.count("admission: jobs 8->2") == 1
        assert recorder.calls == [["python", "eval.py", "--jobs", "2"]]

    def test_a_lowered_start_rewrites_the_eval_jobs_spelling(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code, recorder = _run(
            ["--peak-mb", "600", "--", "python", "audit.py", "--eval-jobs", "8"],
            free_gib=13.0,
            tmp_path=tmp_path,
        )

        assert code == 0
        assert "admission: jobs 8->1" in capsys.readouterr().out
        assert recorder.calls == [["python", "audit.py", "--eval-jobs", "1"]]

    def test_a_job_that_fits_prints_nothing(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code, recorder = _run(
            ["--peak-mb", "600", "--", "python", "eval.py", "--jobs", "8"], tmp_path=tmp_path
        )

        captured = capsys.readouterr()
        assert code == 0
        assert captured.out == ""
        assert captured.err == ""
        assert recorder.calls == [["python", "eval.py", "--jobs", "8"]]

    def test_an_overcommitted_single_worker_still_says_so(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code, recorder = _run(
            ["--peak-mb", "8800", "--", "python", "eval.py", "--jobs", "1"], tmp_path=tmp_path
        )

        captured = capsys.readouterr()
        assert code == 0
        assert "admission: jobs 1 (overcommitted, still starting one" in captured.out
        assert recorder.calls == [["python", "eval.py", "--jobs", "1"]]
        event = json.loads((tmp_path / "events.jsonl").read_text().splitlines()[0])
        assert (event["kind"], event["requested"], event["jobs"]) == ("overcommitted", 1, 1)

    def test_a_single_worker_that_fits_prints_nothing(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code, recorder = _run(
            ["--peak-mb", "600", "--", "python", "eval.py", "--jobs", "1"], tmp_path=tmp_path
        )

        captured = capsys.readouterr()
        assert code == 0
        assert (captured.out, captured.err) == ("", "")
        assert recorder.calls == [["python", "eval.py", "--jobs", "1"]]

    def test_a_missing_declaration_fails_fast(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code, recorder = _run(["--", "python", "eval.py", "--jobs", "8"], tmp_path=tmp_path)

        assert code == 2
        assert "declares no memory" in capsys.readouterr().err
        assert recorder.calls == []

    def test_an_unstable_disk_read_keeps_the_requested_concurrency(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code, recorder = _run(
            ["--peak-mb", "2800", "--", "python", "eval.py", "--jobs", "8"],
            unstable=True,
            tmp_path=tmp_path,
        )

        assert code == 0
        assert "unstable sample" in capsys.readouterr().err
        assert recorder.calls == [["python", "eval.py", "--jobs", "8"]]

    def test_a_dry_run_prints_the_plan_and_starts_nothing(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code, recorder = _run(
            ["--peak-mb", "2800", "--dry-run", "--", "python", "eval.py", "--jobs", "8"],
            tmp_path=tmp_path,
        )

        assert code == 0
        assert "would start 2 of 8" in capsys.readouterr().out
        assert recorder.calls == []

    def test_the_adjusted_start_leaves_one_event(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _run(["--peak-mb", "2800", "--", "python", "eval.py", "--jobs", "8"], tmp_path=tmp_path)
        capsys.readouterr()

        event = json.loads((tmp_path / "events.jsonl").read_text().splitlines()[0])

        assert event["kind"] == "adjusted"
        assert event["script"] == "eval.py"
        assert (event["requested"], event["jobs"]) == (8, 2)


class TestBypass:
    def test_a_bypass_without_a_reason_is_refused(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code, recorder = _run(
            ["--no-admission", "  ", "--", "python", "eval.py"], tmp_path=tmp_path
        )

        assert code == 2
        assert "needs a reason" in capsys.readouterr().err
        assert recorder.calls == []

    def test_a_bypass_runs_unchanged_and_records_the_reason(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code, recorder = _run(
            ["--no-admission", "known-good small run", "--", "python", "eval.py", "--jobs", "8"],
            free_gib=13.0,
            tmp_path=tmp_path,
        )

        assert code == 0
        assert capsys.readouterr().out == ""
        assert recorder.calls == [["python", "eval.py", "--jobs", "8"]]
        event = json.loads((tmp_path / "events.jsonl").read_text().splitlines()[0])
        assert event["kind"] == "bypassed"
        assert event["reason"] == "known-good small run"


class TestLinuxReaders:
    def test_memavailable_is_the_budget(self, tmp_path: Path) -> None:
        meminfo = tmp_path / "meminfo"
        meminfo.write_text("MemTotal:       32800000 kB\nMemAvailable:   24117248 kB\n")

        reading = admission.read_memory(str(meminfo))

        assert reading is not None
        assert reading.source == "memory"
        assert reading.free_gib == pytest.approx(23.0)
        assert reading.unstable is False

    def test_a_missing_meminfo_is_unknown_not_zero(self, tmp_path: Path) -> None:
        assert admission.read_memory(str(tmp_path / "absent")) is None

    def test_meminfo_without_memavailable_is_unknown(self, tmp_path: Path) -> None:
        meminfo = tmp_path / "meminfo"
        meminfo.write_text("MemTotal:       32800000 kB\n")

        assert admission.read_memory(str(meminfo)) is None

    def test_psi_is_recorded_and_absent_when_the_kernel_has_none(self, tmp_path: Path) -> None:
        psi = tmp_path / "memory"
        psi.write_text("some avg10=1.25 avg60=0.50 avg300=0.10 total=1234\nfull avg10=0.00\n")

        assert admission.read_psi(str(psi)) == pytest.approx(1.25)
        assert admission.read_psi(str(tmp_path / "absent")) is None

    def test_gpu_usage_and_capacity_come_from_the_largest_device(self) -> None:
        assert admission.read_gpu(("printf", "0, 8188\n3712, 8188\n")) == (3712, 8188)

    def test_gpu_is_unknown_without_the_tool(self) -> None:
        assert admission.read_gpu(("false",)) is None

    def test_an_absent_budget_reader_keeps_the_requested_concurrency(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        recorder = _Recorder()
        code = admission.main(
            ["--peak-mb", "2800", "--", "python", "eval.py", "--jobs", "8"],
            budget_reader=lambda: None,
            pressure_reader=lambda: 1,
            psi_reader=lambda: None,
            gpu_reader=lambda: None,
            executor=recorder,
            home=tmp_path,
        )

        assert code == 0
        assert "no budget reading" in capsys.readouterr().err
        assert recorder.calls == [["python", "eval.py", "--jobs", "8"]]
        event = json.loads((tmp_path / "events.jsonl").read_text().splitlines()[0])
        assert event["kind"] == "unknown"

    def test_the_adjusted_event_carries_the_gpu_and_psi_readings(self, tmp_path: Path) -> None:
        recorder = _Recorder()
        admission.main(
            ["--peak-mb", "2800", "--", "python", "eval.py", "--jobs", "8"],
            budget_reader=lambda: admission.Reading(
                free_gib=26.1, source="memory", unstable=False, detail="MemAvailable"
            ),
            pressure_reader=lambda: 1,
            psi_reader=lambda: 1.25,
            gpu_reader=lambda: (3712.0, 8188.0),
            executor=recorder,
            home=tmp_path,
        )

        event = json.loads((tmp_path / "events.jsonl").read_text().splitlines()[0])
        assert event["budget_source"] == "memory"
        assert event["psi_some_avg10"] == 1.25
        assert event["gpu_max_used_mib"] == 3712.0
        assert event["gpu_total_mib"] == 8188.0


class TestReservations:
    def _entry(
        self, tmp_path: Path, *, pid: int, peak_mb: float, jobs: int = 1, boot: str | None = None
    ) -> Path:
        directory = tmp_path / "active"
        directory.mkdir(parents=True, exist_ok=True)
        entry = {
            "pid": pid,
            "boot_id": boot if boot is not None else admission.current_boot_id(),
            "script": "run_pilot.py",
            "peak_mb": peak_mb,
            "jobs": jobs,
            "started_at": "2026-10-20T14:20:31+08:00",
        }
        path = directory / f"{pid}.json"
        path.write_text(json.dumps(entry))
        return path

    def test_a_live_reservation_is_subtracted_from_memory_room(self, tmp_path: Path) -> None:
        self._entry(tmp_path, pid=os.getpid(), peak_mb=5120)

        live = admission.live_reservations(tmp_path)

        assert admission.reserved_gib(live) == pytest.approx(5.0)

    def test_a_dead_process_is_pruned_and_stops_counting(self, tmp_path: Path) -> None:
        path = self._entry(tmp_path, pid=999_999_999, peak_mb=5120)

        assert admission.live_reservations(tmp_path) == []
        assert not path.exists()

    def test_another_boot_is_pruned(self, tmp_path: Path) -> None:
        path = self._entry(tmp_path, pid=os.getpid(), peak_mb=5120, boot="some-older-boot")

        assert admission.live_reservations(tmp_path) == []
        assert not path.exists()

    # meiva:note
    # 20 GiB free - 5 GiB reserved - 12 GiB reserve = 3 GiB for 4 GiB/worker x 2.0 = 8 GiB.
    def test_room_is_reduced_before_the_plan_is_made(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        self._entry(tmp_path, pid=os.getpid(), peak_mb=5120)
        recorder = _Recorder()
        code = admission.main(
            ["--peak-mb", "4096", "--", "python", "eval.py", "--jobs", "2"],
            budget_reader=lambda: admission.Reading(
                free_gib=20.0, source="memory", unstable=False, detail="MemAvailable"
            ),
            pressure_reader=lambda: 1,
            psi_reader=lambda: None,
            gpu_reader=lambda: None,
            executor=recorder,
            home=tmp_path,
        )

        assert code == 0
        assert "jobs 2->1" in capsys.readouterr().out
        assert recorder.calls == [["python", "eval.py", "--jobs", "1"]]


class TestClassLock:
    def _holder(self, tmp_path: Path, pid: int) -> Path:
        path = admission.lock_path(tmp_path, "run_pilot.py")
        path.write_text(
            json.dumps(
                {
                    "pid": pid,
                    "boot_id": admission.current_boot_id(),
                    "script": "run_pilot.py",
                    "peak_mb": 11264,
                    "jobs": 1,
                    "started_at": "2026-10-20T14:20:31+08:00",
                }
            )
        )
        return path

    def test_a_big_job_waits_for_its_class_and_names_the_holder(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        self._holder(tmp_path, os.getpid())
        sleeps: list[float] = []

        outcome, waited = admission.wait_for_lock(
            admission.lock_path(tmp_path, "run_pilot.py"),
            {"pid": os.getpid(), "boot_id": admission.current_boot_id(), "script": "run_pilot.py"},
            max_wait=0.0,
            sleep=sleeps.append,
        )

        assert outcome == "timeout"
        assert waited >= 0

    def test_a_stale_lock_is_taken_over(self, tmp_path: Path) -> None:
        path = self._holder(tmp_path, 999_999_999)

        outcome, _ = admission.wait_for_lock(
            path,
            {"pid": os.getpid(), "boot_id": admission.current_boot_id(), "script": "run_pilot.py"},
            max_wait=0.0,
            sleep=lambda _s: None,
        )

        assert outcome == "acquired"

    def test_no_wait_records_the_operators_choice(self, tmp_path: Path) -> None:
        self._holder(tmp_path, os.getpid())

        _lock, reservation, status = admission.hold(
            script="run_pilot.py",
            peak_mb=11264,
            jobs=1,
            home=tmp_path,
            events=tmp_path / "events.jsonl",
            max_wait=1800.0,
            no_wait=True,
        )
        try:
            assert status["wait_outcome"] == "abandoned_by_operator"
            assert reservation is not None and reservation.exists()
        finally:
            admission.release((None, reservation, status))

    def test_a_job_below_the_big_job_line_takes_no_lock_and_no_reservation(
        self, tmp_path: Path
    ) -> None:
        lock, reservation, status = admission.hold(
            script="small.py",
            peak_mb=600,
            jobs=2,
            home=tmp_path,
            events=tmp_path / "events.jsonl",
            max_wait=1800.0,
            no_wait=False,
        )

        assert (lock, reservation) == (None, None)
        assert status["wait_outcome"] == "none"


class TestOptionalProbesOnAHostWithoutThem:
    # meiva:note
    # Each of these readings is either recorded and never priced (the GPU) or backed by a
    # second source that already answered (statvfs without `df`): a host that lacks the tool
    # keeps the job the gate decided to start. 2026-10-21 — an absent `nvidia-smi` raised out
    # of `_announce_gate` before the launch and before the event, so on macOS every start the
    # gate lowered died with rc=1 and left no record at all.

    def test_a_missing_gpu_tool_reads_as_unknown(self) -> None:
        assert admission.read_gpu(("/nonexistent/nvidia-smi",)) is None

    def test_a_missing_df_prices_from_statvfs_alone(self, tmp_path: Path) -> None:
        reading = admission.read_disk(str(tmp_path), df_reader=lambda _path: None)

        assert reading.source == "disk"
        assert reading.free_gib > 0
        assert reading.unstable is False
        assert "statvfs" in reading.detail

    def test_a_missing_df_tool_reads_the_same_as_silence(self) -> None:
        assert admission.read_df("/nonexistent/volume") is None

    def test_a_missing_sysctl_reports_unknown_swap(self) -> None:
        assert admission.swap_line(platform="darwin", sysctl=("/nonexistent/sysctl",)) == "unknown"

    def test_a_lowered_job_starts_and_records_without_a_gpu_tool(self, tmp_path: Path) -> None:
        recorder = _Recorder()
        code = admission.main(
            ["--peak-mb", "2800", "--", "python", "eval.py", "--jobs", "8"],
            budget_reader=_collect(26.1)[0],
            pressure_reader=lambda: 1,
            psi_reader=lambda: None,
            gpu_reader=partial(admission.read_gpu, ("/nonexistent/nvidia-smi",)),
            executor=recorder,
            home=tmp_path,
        )

        assert code == 0
        assert recorder.calls == [["python", "eval.py", "--jobs", "2"]]
        event = json.loads((tmp_path / "events.jsonl").read_text().splitlines()[0])
        assert event["kind"] == "adjusted"
        assert event["gpu_max_used_mib"] is None

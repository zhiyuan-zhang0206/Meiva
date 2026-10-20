from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

import pytest

from scripts.host_ops import admission_run as admission

FIXED_DISK = admission.DiskReading(free_gib=26.1, df_pct=84.0, unstable=False)


def _collect(free_gib: float = 26.1, pressure: int = 1, unstable: bool = False):
    def reader() -> admission.DiskReading:
        return admission.DiskReading(free_gib=free_gib, df_pct=84.0, unstable=unstable)

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
        disk_reader=disk,
        pressure_reader=pressure_reader,
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

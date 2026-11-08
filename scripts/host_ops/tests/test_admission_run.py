from __future__ import annotations

from pathlib import Path

import pytest

from scripts.host_ops import admission_run as admission
from scripts.host_ops.tests.admission_support import Recorder


class TestCommandReading:
    def test_the_job_key_is_the_script_not_the_interpreter(self) -> None:
        runner = "scripts/host_ops/admission_run.py"

        assert admission.job_key([".venv/bin/python", runner, "--jobs", "8"]) == "admission_run.py"
        assert admission.job_key(["/usr/bin/uv", "run", "python", runner]) == "admission_run.py"
        assert admission.job_key(["/usr/bin/python3", "-m", "scripts.host_ops.admission_run"]) == (
            "scripts.host_ops.admission_run"
        )
        assert admission.job_key([]) == ""

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


class TestPassthroughExecution:
    def test_ambient_default_is_the_host_admission_directory(self) -> None:
        assert admission.admission_home() == Path.home() / ".meiva" / "admission"

    def test_passthrough_runs_command_with_executor(self) -> None:
        recorder = Recorder()
        code = admission.main(
            ["--peak-mb", "2800", "--", "python", "eval.py", "--jobs", "8"],
            executor=recorder,
        )

        assert code == 0
        assert recorder.calls == [["python", "eval.py", "--jobs", "8"]]

    def test_peak_mb_is_optional(self) -> None:
        recorder = Recorder()
        code = admission.main(
            ["--", "python", "eval.py", "--jobs", "4"],
            executor=recorder,
        )

        assert code == 0
        assert recorder.calls == [["python", "eval.py", "--jobs", "4"]]

    def test_flags_without_double_dash_still_pass_command(self) -> None:
        recorder = Recorder()
        code = admission.main(
            ["--jobs", "2", "python", "script.py"],
            executor=recorder,
        )

        assert code == 0
        assert recorder.calls == [["python", "script.py"]]

    def test_dry_run_does_not_execute_command(self, capsys: pytest.CaptureFixture[str]) -> None:
        recorder = Recorder()
        code = admission.main(
            ["--dry-run", "--jobs", "4", "--", "python", "eval.py"],
            executor=recorder,
        )

        assert code == 0
        assert recorder.calls == []
        out = capsys.readouterr().out
        assert "admission: dry run — would start 4 of 4 workers" in out

    def test_empty_command_returns_zero(self) -> None:
        recorder = Recorder()
        code = admission.main([], executor=recorder)
        assert code == 0
        assert recorder.calls == []

    def test_no_admission_flag_is_accepted(self) -> None:
        recorder = Recorder()
        code = admission.main(
            ["--no-admission", "urgent maintenance", "--", "echo", "done"],
            executor=recorder,
        )
        assert code == 0
        assert recorder.calls == [["echo", "done"]]

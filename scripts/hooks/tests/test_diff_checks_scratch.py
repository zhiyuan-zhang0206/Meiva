# meiva:note
# Only synthetic scratch and owned short Python processes; no Postgres.

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from scripts.hooks.diff_checks_pytest_child import PytestChildUnreapedError, run_pytest_child
from scripts.hooks.diff_checks_scratch import pytest_scratch


def test_two_runs_only_remove_their_own_scratch(tmp_path: Path) -> None:
    with pytest_scratch(tmp_path) as second:
        with pytest_scratch(tmp_path) as first:
            _ = (first / "a").write_text("a")
            _ = (second / "b").write_text("b")
            assert first != second
        assert not first.exists()
        assert (second / "b").read_text() == "b"
    assert not first.exists() and not second.exists()


def test_exception_removes_readonly_tree_without_following_link(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "keep"
    _ = sentinel.write_text("foreign")
    with pytest.raises(ValueError), pytest_scratch(tmp_path) as scratch:
        protected = scratch / "protected"
        protected.mkdir()
        _ = (protected / "file").write_text("owned")
        protected.chmod(0o500)
        (scratch / "link").symlink_to(outside, target_is_directory=True)
        raise ValueError("test failure")
    assert not scratch.exists()
    assert sentinel.read_text() == "foreign"


def test_unconfirmed_child_retains_owned_scratch(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(PytestChildUnreapedError), pytest_scratch(tmp_path) as scratch:
        raise PytestChildUnreapedError("synthetic wait failure")
    assert scratch.is_dir()
    assert "retained scratch" in capsys.readouterr().err
    scratch.rmdir()


def test_replaced_scratch_name_is_not_removed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest_scratch(tmp_path) as scratch:
        moved = scratch.with_name(scratch.name + "-moved")
        _ = scratch.rename(moved)
        scratch.mkdir()
        _ = (scratch / "new-owner").write_text("keep")
    assert (scratch / "new-owner").exists()
    assert "replaced name" in capsys.readouterr().err


@pytest.mark.parametrize("code", [0, 7])
def test_child_exit_and_output_are_preserved(tmp_path: Path, code: int) -> None:
    result, output, _ = run_pytest_child(
        (sys.executable, "-m", "scripts.hooks.tests.child_diff_checks_scratch", "exit", str(code)),
        tmp_path,
        environment={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[3])},
    )
    assert result == code and output.strip() == "diagnostic"


def test_parent_term_waits_for_owned_child(tmp_path: Path) -> None:
    pid_file = tmp_path / "child.pid"
    run_file = tmp_path / "run.path"
    # meiva:note
    # synthetic test-owned module
    owner = subprocess.Popen(  # noqa: S603
        [
            sys.executable,
            "-m",
            "scripts.hooks.tests.child_diff_checks_scratch",
            "owner",
            str(tmp_path),
            str(pid_file),
            str(run_file),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 10
        while not pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert pid_file.exists()
        pid = int(pid_file.read_text())
        owner.send_signal(signal.SIGTERM)
        _ = owner.communicate(timeout=10)
        assert owner.returncode == -signal.SIGTERM
        assert not Path(run_file.read_text()).exists()
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    finally:
        if owner.poll() is None:
            owner.terminate()
        _ = owner.communicate(timeout=10)


def test_kill_leaves_scratch_and_child_until_explicit_test_owner_cleanup(tmp_path: Path) -> None:
    import psutil

    pid_file = tmp_path / "child.pid"
    run_file = tmp_path / "run.path"
    # meiva:note
    # synthetic test-owned module
    owner = subprocess.Popen(  # noqa: S603
        [
            sys.executable,
            "-m",
            "scripts.hooks.tests.child_diff_checks_scratch",
            "owner",
            str(tmp_path),
            str(pid_file),
            str(run_file),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    owned: psutil.Process | None = None
    try:
        deadline = time.monotonic() + 10
        while not pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert pid_file.exists()
        owned = psutil.Process(int(pid_file.read_text()))
        assert owned.ppid() == owner.pid
        owner.kill()
        _ = owner.communicate(timeout=10)
        assert owned.is_running()
        assert Path(run_file.read_text()).is_dir(), "KILL cannot run the owner's finally"
    finally:
        if owner.poll() is None:
            owner.terminate()
        _ = owner.communicate(timeout=10)
        if owned is not None and owned.is_running():
            owned.terminate()
            _ = owned.wait(timeout=10)


def test_failed_run_emits_small_evidence_before_basetemp_removal(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from collections.abc import Sequence

    from scripts.hooks.diff_checks import ChangeSet, Selection, check

    seen: list[Path] = []

    def execute(argv: Sequence[str], _root: Path) -> tuple[int, str, float]:
        if tuple(argv[1:3]) != ("-m", "pytest"):
            return 0, "0 errors, 0 warnings", 0.1
        report = Path(next(a.split("=", 1)[1] for a in argv if a.startswith("--junitxml=")))
        basetemp = Path(next(a.split("=", 1)[1] for a in argv if a.startswith("--basetemp=")))
        basetemp.mkdir()
        _ = (basetemp / "large-tree-placeholder").write_text("owned")
        _ = report.write_text(
            '<testsuites><testsuite><testcase name="synthetic-failure"><failure /></testcase></testsuite></testsuites>'
        )
        seen.append(report.parent)
        return 1, "synthetic-failure-log", 0.1

    def choose(_change: ChangeSet) -> Selection:
        return Selection("SELECTED", ("tests/test_synthetic.py",), ())

    values, failed = check(
        ChangeSet(root=tmp_path, base="origin/main", files=("base/thing.py",)),
        skip_tests=False,
        execute=execute,
        python=sys.executable,
        choose=choose,
    )
    assert failed and values["failed"] == "1tests"
    assert seen and not seen[0].exists()
    evidence = capsys.readouterr().err
    assert "synthetic-failure-log" in evidence and '<testcase name="synthetic-failure">' in evidence


def test_stopping_one_owner_preserves_the_other_run(tmp_path: Path) -> None:
    import psutil

    owners: list[subprocess.Popen[bytes]] = []
    runs: list[Path] = []
    pids: list[int] = []
    try:
        for number in (1, 2):
            parent = tmp_path / str(number)
            parent.mkdir(mode=0o700)
            pid_file = tmp_path / f"{number}.pid"
            run_file = tmp_path / f"{number}.run"
            # meiva:note
            # synthetic test-owned module
            owner = subprocess.Popen(  # noqa: S603
                [
                    sys.executable,
                    "-m",
                    "scripts.hooks.tests.child_diff_checks_scratch",
                    "owner",
                    str(parent),
                    str(pid_file),
                    str(run_file),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            owners.append(owner)
            deadline = time.monotonic() + 10
            while not pid_file.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert pid_file.exists()
            pids.append(int(pid_file.read_text()))
            runs.append(Path(run_file.read_text()))
        owners[0].terminate()
        _ = owners[0].communicate(timeout=10)
        assert owners[0].returncode == -signal.SIGTERM and not runs[0].exists()
        assert psutil.Process(pids[1]).ppid() == owners[1].pid
        _ = (runs[1] / "still-owned").write_text("B")
        assert (runs[1] / "still-owned").read_text() == "B"
    finally:
        for owner in owners:
            if owner.poll() is None:
                owner.terminate()
            _ = owner.communicate(timeout=10)


def test_unreadable_report_diagnostic_does_not_mask_interruption(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from scripts.hooks.diff_checks_pytest import pytest_failure_evidence

    pytest_failure_evidence(tmp_path, "original interruption")
    output = capsys.readouterr().err
    assert "original interruption" in output and "JUnit unavailable" in output

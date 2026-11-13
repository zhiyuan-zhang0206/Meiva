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
from scripts.hooks.diff_checks_scratch import ScratchCleanupError, pytest_scratch


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
    with pytest.raises(ScratchCleanupError), pytest_scratch(tmp_path) as scratch:
        moved = scratch.with_name(scratch.name + "-moved")
        _ = scratch.rename(moved)
        scratch.mkdir()
        _ = (scratch / "new-owner").write_text("keep")
    assert (scratch / "new-owner").exists()
    assert "cleanup incomplete" in capsys.readouterr().err


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
    admitted = False
    try:
        deadline = time.monotonic() + 10
        while not pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert pid_file.exists()
        owned = psutil.Process(int(pid_file.read_text()))
        assert owned.ppid() == owner.pid
        assert os.getpgid(owned.pid) == os.getsid(owned.pid) == owned.pid
        assert owned.cmdline()[1:] == [
            "-m",
            "scripts.hooks.tests.child_diff_checks_scratch",
            "sleep",
            str(pid_file),
        ]
        admitted = True
        owner.kill()
        _ = owner.communicate(timeout=10)
        assert owned.is_running()
        assert Path(run_file.read_text()).is_dir(), "KILL cannot run the owner's finally"
    finally:
        if owner.poll() is None:
            owner.terminate()
        _ = owner.communicate(timeout=10)
        if admitted and owned is not None and owned.is_running():
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


def test_run_scratch_is_outside_pytest_numbered_cleanup_candidates(tmp_path: Path) -> None:
    from _pytest.pathlib import cleanup_candidates

    (tmp_path / "pytest-100").mkdir()
    with pytest_scratch(tmp_path) as scratch:
        assert scratch not in set(cleanup_candidates(tmp_path, prefix="pytest-", keep=3))


@pytest.mark.parametrize("changed_index", [1, 2, 4])
def test_directory_guard_rejects_changed_inode_device_or_uid(changed_index: int) -> None:
    import stat

    from scripts.hooks.diff_checks_scratch import require_owned_directory

    expected = os.stat_result((stat.S_IFDIR | 0o700, 11, 22, 1, 501, 501, 0, 0, 0, 0))
    require_owned_directory(expected, expected, 501)
    candidate = list(expected)
    candidate[changed_index] += 1
    with pytest.raises(PermissionError):
        require_owned_directory(os.stat_result(candidate), expected, 501)


def test_constructor_error_preserves_deferred_term_and_restores_handler(tmp_path: Path) -> None:
    from collections.abc import Mapping, Sequence

    from scripts.hooks.diff_checks_pytest_child import PytestInterrupted

    previous = signal.getsignal(signal.SIGTERM)
    starts: list[bool] = []

    def fail(
        argv: Sequence[str], root: Path, environment: Mapping[str, str]
    ) -> subprocess.Popen[str]:
        del argv, root, environment
        starts.append(True)
        os.kill(os.getpid(), signal.SIGTERM)
        raise OSError("synthetic constructor failure")

    with pytest.raises(PytestInterrupted) as result:
        _ = run_pytest_child((sys.executable,), tmp_path, environment={}, start=fail)
    assert starts == [True]
    assert result.value.signum == signal.SIGTERM
    assert isinstance(result.value.__cause__, OSError)
    assert signal.getsignal(signal.SIGTERM) == previous


def test_worker_thread_refuses_before_child_construction(tmp_path: Path) -> None:
    from collections.abc import Mapping, Sequence
    from threading import Thread

    starts: list[bool] = []
    errors: list[BaseException] = []
    previous = signal.getsignal(signal.SIGTERM)

    def fail(
        argv: Sequence[str], root: Path, environment: Mapping[str, str]
    ) -> subprocess.Popen[str]:
        del argv, root, environment
        starts.append(True)
        raise AssertionError("worker thread must not construct a child")

    def worker() -> None:
        try:
            _ = run_pytest_child((sys.executable,), tmp_path, environment={}, start=fail)
        except ValueError as error:
            errors.append(error)

    thread = Thread(target=worker)
    thread.start()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert len(errors) == 1 and not starts
    assert signal.getsignal(signal.SIGTERM) == previous


def test_same_session_descendant_retains_scratch_after_leader_exit(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import psutil

    from scripts.hooks.tests.child_diff_checks_scratch import DescendantAnchor

    record = tmp_path / "descendant.json"
    pid_file = tmp_path / "descendant.pid"
    scratch_path: Path | None = None
    try:
        with pytest.raises(PytestChildUnreapedError), pytest_scratch(tmp_path) as scratch:
            scratch_path = scratch
            _ = run_pytest_child(
                (
                    sys.executable,
                    "-m",
                    "scripts.hooks.tests.child_diff_checks_scratch",
                    "leader",
                    str(record),
                    str(pid_file),
                ),
                Path.cwd(),
                environment=dict(os.environ),
            )
        anchor = DescendantAnchor.model_validate_json(record.read_text())
        owned = psutil.Process(anchor.pid)
        assert anchor.group == anchor.session == anchor.leader
        assert owned.create_time() == anchor.birth
        assert os.getpgid(owned.pid) == anchor.group
        assert owned.is_running() and scratch.is_dir()
        assert "retained scratch" in capsys.readouterr().err
    finally:
        if record.exists():
            anchor = DescendantAnchor.model_validate_json(record.read_text())
            owned = psutil.Process(anchor.pid)
            assert owned.create_time() == anchor.birth, "unknown identity; no signal"
            assert os.getpgid(owned.pid) == anchor.group, "unknown group; no signal"
            assert owned.cmdline()[1:] == anchor.argv[1:], "unknown command; no signal"
            owned.terminate()
            _ = owned.wait(timeout=10)
        if scratch_path is not None and scratch_path.exists():
            scratch_path.rmdir()


def test_swapped_opened_directory_is_preserved_before_chmod(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import stat

    substitute = tmp_path / "substitute"
    substitute.mkdir(mode=0o700)
    _ = (substitute / "keep").write_text("synthetic foreign content")

    def exchange(name: str, parent_fd: int) -> int:
        _ = os.rename(name, name + "-original", src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        _ = os.rename(substitute, name, dst_dir_fd=parent_fd)
        os.chmod(name, 0o500, dir_fd=parent_fd, follow_symlinks=False)
        return os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)

    scratch_path: Path | None = None
    try:
        with (
            pytest.raises(ScratchCleanupError),
            pytest_scratch(tmp_path, open_child=exchange) as scratch,
        ):
            scratch_path = scratch
            (scratch / "selected").mkdir()
        replaced = scratch / "selected"
        assert (replaced / "keep").read_text() == "synthetic foreign content"
        assert stat.S_IMODE(replaced.stat().st_mode) == 0o500
        assert "cleanup incomplete" in capsys.readouterr().err
    finally:
        if scratch_path is not None and scratch_path.exists():
            if (scratch_path / "selected").exists():
                (scratch_path / "selected").chmod(0o700)
                _ = (scratch_path / "selected").rename(substitute)
            if (scratch_path / "selected-original").exists():
                (scratch_path / "selected-original").rmdir()
            scratch_path.rmdir()
        if substitute.exists():
            substitute.chmod(0o700)


@pytest.mark.parametrize("kind", ["TERM", "INT"])
def test_unknown_cleanup_preserves_primary_interrupt_and_scratch(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], kind: str
) -> None:
    from collections.abc import Mapping, Sequence

    from scripts.hooks.diff_checks_pytest_child import PytestInterrupted

    previous = signal.getsignal(signal.SIGTERM)

    def fail_start(
        argv: Sequence[str], root: Path, environment: Mapping[str, str]
    ) -> subprocess.Popen[str]:
        del argv, root, environment
        if kind == "TERM":
            os.kill(os.getpid(), signal.SIGTERM)
            raise OSError("synthetic constructor failure")
        raise KeyboardInterrupt

    def unknown_stop(child: subprocess.Popen[str] | None) -> None:
        assert child is None
        raise PytestChildUnreapedError("synthetic observation unknown")

    expected = PytestInterrupted if kind == "TERM" else KeyboardInterrupt
    with pytest.raises(expected) as result, pytest_scratch(tmp_path) as scratch:
        _ = run_pytest_child(
            (sys.executable,), tmp_path, environment={}, start=fail_start, stop=unknown_stop
        )
    assert isinstance(result.value.__cause__, PytestChildUnreapedError)
    assert signal.getsignal(signal.SIGTERM) == previous
    assert scratch.is_dir()
    output = capsys.readouterr().err
    assert "cleanup incomplete during interruption" in output and "retained scratch" in output
    scratch.rmdir()


@pytest.mark.parametrize("kind", ["TERM", "INT"])
def test_directory_cleanup_error_keeps_original_interrupt(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], kind: str
) -> None:
    from scripts.hooks.diff_checks_pytest_child import PytestInterrupted

    def refuse(_name: str, _parent: int) -> int:
        raise PermissionError("synthetic directory observation failure")

    interruption = PytestInterrupted(signal.SIGTERM) if kind == "TERM" else KeyboardInterrupt()
    with (
        pytest.raises(type(interruption)) as result,
        pytest_scratch(tmp_path, open_child=refuse) as scratch,
    ):
        (scratch / "keep").mkdir()
        raise interruption
    assert result.value is interruption
    assert (scratch / "keep").is_dir()
    assert "cleanup incomplete" in capsys.readouterr().err
    (scratch / "keep").rmdir()
    scratch.rmdir()


@pytest.mark.parametrize("kind, signum", [("TERM", signal.SIGTERM), ("INT", signal.SIGINT)])
def test_cleanup_failure_cli_retains_signal_exit(tmp_path: Path, kind: str, signum: int) -> None:
    record = tmp_path / "run.path"
    # meiva:note
    # This isolated fixture synthesizes unknown cleanup without creating an external child.
    result = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-m",
            "scripts.hooks.tests.child_diff_checks_scratch",
            "cleanup-interrupt",
            str(tmp_path),
            str(record),
            kind,
        ],
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == -signum
    scratch = Path(record.read_text())
    assert scratch.is_dir() and b"retained scratch" in result.stderr
    scratch.rmdir()


@pytest.mark.parametrize("unknown", [False, True])
def test_term_received_during_cleanup_is_recorded_and_replayed(
    tmp_path: Path, unknown: bool
) -> None:
    from collections.abc import Mapping, Sequence

    from scripts.hooks.diff_checks_pytest_child import PytestInterrupted

    previous = signal.getsignal(signal.SIGTERM)

    def fail_start(
        argv: Sequence[str], root: Path, environment: Mapping[str, str]
    ) -> subprocess.Popen[str]:
        del argv, root, environment
        raise OSError("synthetic constructor failure before interruption")

    def stop(child: subprocess.Popen[str] | None) -> None:
        assert child is None
        os.kill(os.getpid(), signal.SIGTERM)
        if unknown:
            raise PytestChildUnreapedError("synthetic unknown cleanup")

    with pytest.raises(PytestInterrupted) as result, pytest_scratch(tmp_path) as scratch:
        _ = run_pytest_child(
            (sys.executable,), tmp_path, environment={}, start=fail_start, stop=stop
        )
    assert result.value.signum == signal.SIGTERM
    assert signal.getsignal(signal.SIGTERM) == previous
    assert scratch.exists() == unknown
    if unknown:
        scratch.rmdir()

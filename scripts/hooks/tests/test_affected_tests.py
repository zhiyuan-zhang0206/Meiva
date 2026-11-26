from __future__ import annotations

import os
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from scripts.hooks.affected_tests import (
    EXIT_NOT_RUN,
    EXIT_PASS,
    EXIT_TEST_FAILURE,
    Selection,
    changed_files,
    child_environment,
    main,
    merge_base,
    parse_args,
    pytest_argv,
    run_pytest,
)

PYTHON = sys.executable
REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
_GIT_ENVIRONMENT = {"PATH": os.environ["PATH"], "HOME": "/nonexistent"}


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(  # noqa: S603
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
        env=_GIT_ENVIRONMENT,
    )
    return done.stdout.strip()


def _repo_with_base(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "kept.py").write_text("kept = 1\n")
    (repo / "gone.py").write_text("gone = 1\n")
    (repo / "edited.py").write_text("edited = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    return repo, _git(repo, "rev-parse", "HEAD")


class Probe:
    def __init__(self, selection: Selection, code: int | None = 0) -> None:
        self.selection = selection
        self.code = code
        self.selected_with: tuple[Sequence[str], str] | None = None
        self.ran: tuple[Sequence[str], Mapping[str, str], int] | None = None

    def select(self, _root: Path, files: Sequence[str], base_sha: str) -> Selection:
        self.selected_with = (tuple(files), base_sha)
        return self.selection

    def run(
        self, _root: Path, argv: Sequence[str], environment: Mapping[str, str], timeout: int
    ) -> int | None:
        self.ran = (tuple(argv), dict(environment), timeout)
        return self.code


def _drive(
    tmp_path: Path, probe: Probe, *flags: str, environment: Mapping[str, str] | None = None
) -> int:
    repo, base = _repo_with_base(tmp_path)
    (repo / "edited.py").write_text("edited = 2\n")
    return main(
        ["--base", base, "--repo-root", str(repo), *flags],
        parent_environment=environment or {"PATH": os.environ["PATH"]},
        python=PYTHON,
        selector=probe.select,
        runner=probe.run,
    )


def _selected(*tests: str) -> Selection:
    return Selection(decision="SELECTED", reason="owner-tests", tests=tests, blind=())


def test_changed_files_cover_commits_edits_deletions_and_untracked_files(tmp_path: Path) -> None:
    repo, base = _repo_with_base(tmp_path)
    (repo / "committed.py").write_text("committed = 1\n")
    _git(repo, "add", "committed.py")
    _git(repo, "commit", "-qm", "committed")
    (repo / "edited.py").write_text("edited = 2\n")
    (repo / "gone.py").unlink()
    (repo / "untracked.py").write_text("untracked = 1\n")
    (repo / "with space.py").write_text("spaced = 1\n")
    assert changed_files(repo, base) == (
        "committed.py",
        "edited.py",
        "gone.py",
        "untracked.py",
        "with space.py",
    )


def test_an_explicit_base_resolves_to_its_commit(tmp_path: Path) -> None:
    repo, base = _repo_with_base(tmp_path)
    assert merge_base(repo, "HEAD", {}) == base


def test_an_unknown_base_is_reported_not_swallowed(tmp_path: Path) -> None:
    repo, _ = _repo_with_base(tmp_path)
    with pytest.raises(RuntimeError, match="rev-parse"):
        merge_base(repo, "no-such-ref", {})


def test_the_children_do_not_inherit_the_operators_meiva_environment() -> None:
    parent = {"MEIVA_HOME": "/operator/home", "HOME": "/operator", "PATH": "/bin"}
    assert child_environment(parent) == {"HOME": "/operator", "PATH": "/bin"}


def test_pytest_is_started_with_bounded_workers_and_no_cache_directory() -> None:
    argv = pytest_argv(["a/test_a.py", "b/test_b.py"], python="py", workers=3)
    assert argv[:3] == ["py", "-m", "pytest"]
    assert "-n=3" in argv
    assert "no:cacheprovider" in argv
    assert argv[-3:] == ["--", "a/test_a.py", "b/test_b.py"]


@pytest.mark.parametrize("workers", ["0", "5"])
def test_a_worker_count_outside_the_cap_is_a_usage_error(workers: str) -> None:
    with pytest.raises(SystemExit) as raised:
        parse_args(["--workers", workers])
    assert raised.value.code == 2


def test_nothing_changed_runs_nothing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo, base = _repo_with_base(tmp_path)
    probe = Probe(_selected("tests/test_x.py"))
    code = main(
        ["--base", base, "--repo-root", str(repo)],
        parent_environment={"PATH": os.environ["PATH"]},
        python=PYTHON,
        selector=probe.select,
        runner=probe.run,
    )
    assert code == EXIT_PASS
    assert probe.selected_with is None
    assert "PASS nothing differs" in capsys.readouterr().out


def test_a_selection_runs_pytest_once_and_passes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    probe = Probe(_selected("tests/test_x.py"), code=0)
    code = _drive(
        tmp_path,
        probe,
        "--workers",
        "2",
        "--timeout",
        "90",
        environment={"PATH": os.environ["PATH"], "MEIVA_HOME": "/operator/home"},
    )
    assert code == EXIT_PASS
    assert probe.selected_with is not None
    assert probe.selected_with[0] == ("edited.py",)
    assert probe.ran is not None
    argv, environment, timeout = probe.ran
    assert argv[-2:] == ("--", "tests/test_x.py")
    assert "MEIVA_HOME" not in environment
    assert timeout == 90
    assert "PASS 1 test file(s)" in capsys.readouterr().out


def test_a_failing_run_exits_nonzero_and_names_where_to_look(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = _drive(tmp_path, Probe(_selected("tests/test_x.py"), code=1))
    assert code == EXIT_TEST_FAILURE
    assert "FAIL pytest exit 1" in capsys.readouterr().err


def test_a_timed_out_run_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = _drive(tmp_path, Probe(_selected("tests/test_x.py"), code=None))
    assert code == EXIT_TEST_FAILURE
    assert "timed out" in capsys.readouterr().err


def test_no_affected_test_is_a_pass_without_running_pytest(tmp_path: Path) -> None:
    probe = Probe(Selection(decision="SKIP", reason="docs-only", tests=(), blind=()))
    assert _drive(tmp_path, probe) == EXIT_PASS
    assert probe.ran is None


def test_a_change_the_selector_cannot_bound_is_not_run_and_not_green(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    probe = Probe(Selection(decision="FULL", reason="unmapped", tests=(), blind=("odd.bin",)))
    assert _drive(tmp_path, probe) == EXIT_NOT_RUN
    assert probe.ran is None
    err = capsys.readouterr().err
    assert "NOT RUN" in err
    assert "odd.bin" in err


def test_a_selection_over_the_file_cap_is_refused(tmp_path: Path) -> None:
    probe = Probe(_selected("a/test_a.py", "b/test_b.py"))
    assert _drive(tmp_path, probe, "--max-files", "1") == EXIT_NOT_RUN
    assert probe.ran is None


def test_a_dry_run_lists_the_selection_and_runs_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    probe = Probe(_selected("a/test_a.py"))
    assert _drive(tmp_path, probe, "--dry-run") == EXIT_PASS
    assert probe.ran is None
    assert "a/test_a.py" in capsys.readouterr().out


def test_run_pytest_returns_the_childs_exit_code() -> None:
    argv = [PYTHON, "-m", "scripts.hooks.tests.child_process", "exit", "3"]
    assert run_pytest(REPOSITORY_ROOT, argv, {}, 60) == 3


def test_a_timeout_stops_the_whole_process_group(tmp_path: Path) -> None:
    marker = tmp_path / "grandchild.pid"
    argv = [PYTHON, "-m", "scripts.hooks.tests.child_process", "tree", str(marker)]
    assert run_pytest(REPOSITORY_ROOT, argv, {}, 3) is None
    grandchild = int(marker.read_text())
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            os.kill(grandchild, 0)
        except ProcessLookupError:
            return
        time.sleep(0.1)
    pytest.fail("the grandchild survived the timeout")

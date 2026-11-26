from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from scripts.hooks.meizel_run import child_environment, index_scan_problem, main

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


def _committed_repo(tmp_path: Path) -> tuple[Path, Path]:
    source = tmp_path / "consumer.py"
    source.write_text("import math\nvalue = math.pi\n")
    os.utime(source, (1_700_000_000, 1_700_000_000))
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "core.trustctime", "false")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "base")
    return tmp_path, source


def _stub_python(tmp_path: Path, exit_code: int) -> Path:
    ran = tmp_path / "stub-ran"
    stub = tmp_path / "stub-python"
    stub.write_text(f"#!/bin/sh\ntouch {ran}\nexit {exit_code}\n")
    stub.chmod(0o755)
    return stub


def test_the_children_do_not_inherit_the_operators_meiva_environment() -> None:
    parent = {"MEIVA_HOME": "/operator/home", "HOME": "/operator", "PATH": "/bin"}
    assert child_environment(parent) == {"HOME": "/operator", "PATH": "/bin"}


def test_a_clean_index_has_no_scan_problem(tmp_path: Path) -> None:
    repo, _ = _committed_repo(tmp_path)
    assert index_scan_problem(repo) is None


def test_bytes_that_differ_from_the_index_are_refused_even_when_git_reports_clean(
    tmp_path: Path,
) -> None:
    repo, source = _committed_repo(tmp_path)
    before = source.stat()
    source.write_bytes(source.read_bytes().replace(b"import math", b"import 0000", 1))
    os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert source.stat().st_size == before.st_size
    assert _git(repo, "status", "--porcelain") == ""
    problem = index_scan_problem(repo)
    assert problem is not None
    assert "working-tree bytes differ from the index for 'consumer.py'" in problem


def test_a_scan_problem_stops_the_run_before_meizel_starts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo, source = _committed_repo(tmp_path)
    before = source.stat()
    source.write_bytes(source.read_bytes().replace(b"import math", b"import 0000", 1))
    os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns))
    stub = _stub_python(tmp_path, 0)
    assert main(repo, python=str(stub), environment={}) == 1
    assert not (tmp_path / "stub-ran").exists()
    assert "working-tree bytes differ" in capsys.readouterr().err


@pytest.mark.parametrize("exit_code", [0, 1, 3])
def test_the_checker_exit_code_is_the_run_exit_code(tmp_path: Path, exit_code: int) -> None:
    repo, _ = _committed_repo(tmp_path)
    stub = _stub_python(tmp_path, exit_code)
    assert main(repo, python=str(stub), environment={}) == exit_code
    assert (tmp_path / "stub-ran").exists()

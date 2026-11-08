from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[4]
_DIR = _ROOT / "scripts/ci/pull_requests"
_SHELL = _DIR / "gate/merge_ready.py"
_SHIM = _DIR / "gate/bin/gh"
_STUB = _DIR / "tests/merge_gate_stub.py"


def _env(tmp_path: Path, **extra: str) -> dict[str, str]:
    grant = tmp_path / "lane-agents.txt"
    grant.write_text("# 3230\n", encoding="utf-8")
    env = {
        **os.environ,
        "MEIVA_GATE_MODULE": str(_STUB),
        "MEIVA_LANE_FILE": str(grant),
        "MERGE_READY_STUB_COUNTER": str(tmp_path / "calls.txt"),
    }
    env.update(extra)
    return env


def _run(tmp_path: Path, argv: list[str], **extra: str) -> subprocess.CompletedProcess[str]:
    # meiva:note
    # Every subprocess in this file runs a file of this repository, by path, or git against a
    # directory the test made; the directives below are this note's.
    return subprocess.run(  # noqa: S603
        [sys.executable, str(_SHELL), *argv],
        capture_output=True,
        text=True,
        env=_env(tmp_path, **extra),
        cwd=tmp_path,
        check=False,
    )


def _calls(tmp_path: Path) -> list[str]:
    path = tmp_path / "calls.txt"
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


def test_the_shell_calls_the_gate_exactly_once(tmp_path: Path) -> None:
    result = _run(tmp_path, ["4242"])

    assert result.returncode == 0, result.stderr
    assert len(_calls(tmp_path)) == 1


# meiva:note
# The gate runs from its own checkout's root, because `gh api repos/{owner}/{repo}` resolves the
# placeholder from the current repository. The stub records the directory it was started in, so the
# assertion is about the module's cwd rather than about a path this test recomputes.
def test_the_gate_runs_from_its_own_checkout_root(tmp_path: Path) -> None:
    _run(tmp_path, ["4242"])

    assert _calls(tmp_path) == [str(_ROOT)]


# meiva:note
# Byte-identical stdout is what lets a lane treat the wrapper's output as the gate's output: a shell
# that reformats what it forwards hides the verdict it claims to report.
def test_stdout_is_byte_identical_to_calling_the_gate_directly(tmp_path: Path) -> None:
    through_shell = _run(tmp_path, ["4242", "--write"])
    direct = subprocess.run(  # noqa: S603
        [sys.executable, str(_STUB), "4242", "--write"],
        capture_output=True,
        text=True,
        env=_env(tmp_path),
        cwd=_ROOT,
        check=False,
    )

    assert through_shell.stdout == direct.stdout
    assert through_shell.returncode == direct.returncode == 0
    assert len(_calls(tmp_path)) == 2


def test_the_shell_returns_the_gates_exit_code(tmp_path: Path) -> None:
    result = _run(tmp_path, ["4242"], MERGE_READY_STUB_RC="3")

    assert result.returncode == 3
    assert len(_calls(tmp_path)) == 1


# meiva:note
# A host with no authority table must refuse, not merge for nobody and look healthy: the refusal is
# what tells an operator the install step was skipped. HOME is redirected so the default path
# resolves inside the test, never against the machine's real grant file.
def test_a_missing_grant_list_fails_closed_without_calling_the_gate(tmp_path: Path) -> None:
    env = _env(tmp_path, HOME=str(tmp_path / "home"))
    env.pop("MEIVA_LANE_FILE")

    result = subprocess.run(  # noqa: S603
        [sys.executable, str(_SHELL), "4242"],
        capture_output=True,
        text=True,
        env=env,
        cwd=tmp_path,
        check=False,
    )

    assert result.returncode != 0
    assert "no lane grant list" in result.stderr
    assert _calls(tmp_path) == []


# meiva:note
# The shim cannot re-derive a merge rule, but it does block --auto, and it must decide correctly
# whether this directory's repository is the code repo at all: a wrong yes dares the gate to judge
# someone else's repository, a wrong no silently disables the gate for the lane.
def test_the_shim_refuses_auto_merge_before_reaching_the_real_gh(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    fake = real / "gh"
    fake.write_text('#!/bin/sh\necho real-gh "$@"\n', encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)  # noqa: S603
    subprocess.run(  # noqa: S603
        ["git", "-C", str(repo), "remote", "add", "origin", "git@github.com:x/y/Meiva.git"],
        check=True,
    )

    # meiva:note
    # The shim is this repository's own file, run by path; its refusal is what the test observes.
    result = subprocess.run(  # noqa: S603
        [str(_SHIM), "pr", "merge", "4242", "--auto"],
        capture_output=True,
        text=True,
        cwd=repo,
        env={**os.environ, "PATH": f"{real}:{os.environ['PATH']}"},
        check=False,
    )

    assert result.returncode == 1
    assert "auto-merge is blocked" in result.stderr
    assert "real-gh" not in result.stdout

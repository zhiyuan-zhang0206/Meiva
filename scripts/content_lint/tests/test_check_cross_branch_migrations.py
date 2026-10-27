"""Tests for scripts/content_lint/check_cross_branch_migrations.py — the single-branch tripwire.

The previous version of this file asserted `main() == 0` unconditionally and that
the script "never touches git". That was an accurate test of a pass-through that
could not fail — which is precisely why the CI step was worthless. These tests
assert the opposite property: that it fires when its premise breaks.
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence

import pytest

from base.host.system.hostport import Host
from base.host.system.tests.fake_commands import fake_host
from scripts.content_lint import check_cross_branch_migrations as mod


@pytest.mark.parametrize(
    "branch",
    ["develop", "development", "staging", "next", "trunk", "release/1.2", "hotfix/x", "support/2"],
)
def test_breach_detected(branch: str) -> None:
    """Every name/prefix denoting a parallel integration line is a breach."""
    assert mod.breaches(["main", branch]) == [branch]


@pytest.mark.parametrize(
    "branch",
    [
        "main",
        "meiva-2201-lint-gate",  # ordinary feature branch — numerous, short-lived
        "meiva-2203-migration-gates",
        "developer-notes",  # contains 'develop', is not that branch
        "release-notes",  # 'release' without the '/' — not a release line
    ],
)
def test_ordinary_branches_are_not_breaches(branch: str) -> None:
    """No heuristic: feature branches and lookalike names must never fire.

    `developer-notes` / `release-notes` guard the exact-match and prefix
    boundaries — a substring test or `startswith("release")` would false-positive
    on both, and a tripwire that cries wolf gets disabled.
    """
    assert mod.breaches([branch]) == []


def _ls_remote_host(stdout: str, returncode: int = 0, stderr: str = "") -> Host:
    def run(
        argv: Sequence[str], *, timeout: float, **options: object
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(list(argv), returncode, stdout=stdout, stderr=stderr)

    return fake_host(run)


# meiva:note
# The git command runs in the environment of the host it is handed, plus git's non-interactive
# knobs, not in the environment of the process that runs the test.
def test_ls_remote_runs_in_the_environment_of_the_host() -> None:
    seen: list[object] = []

    def run(
        argv: Sequence[str], *, timeout: float, **options: object
    ) -> subprocess.CompletedProcess[str]:
        seen.append(options["env"])
        return subprocess.CompletedProcess(list(argv), 0, stdout="", stderr="")

    host = fake_host(run, environ={"ONLY_IN_THE_HOST": "1"})
    assert mod.remote_branches(host=host) == []
    [env] = seen
    assert isinstance(env, dict)
    assert env["ONLY_IN_THE_HOST"] == "1"
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert "PATH" not in env


def test_passes_on_single_branch_model(capsys: pytest.CaptureFixture[str]) -> None:
    """The premise holding is reported, not silently assumed."""
    host = _ls_remote_host("abc\trefs/heads/main\ndef\trefs/heads/meiva-1-x\n")
    assert mod.main(host=host) == 0
    assert "single-branch premise holds" in capsys.readouterr().out


def test_fails_when_premise_breaks(capsys: pytest.CaptureFixture[str]) -> None:
    """A long-lived second branch fails the step and names the remedy."""
    host = _ls_remote_host("abc\trefs/heads/main\ndef\trefs/heads/develop\n")
    assert mod.main(host=host) == 1
    err = capsys.readouterr().err
    assert "premise BROKEN" in err
    assert "develop" in err
    assert "Reinstate" in err


def test_unqueryable_remote_fails_rather_than_passing(capsys: pytest.CaptureFixture[str]) -> None:
    """An unverifiable premise must not report success.

    Returning 0 here would rebuild the silent pass-through this script replaced:
    a gate that cannot answer has to say so, not say 'fine'.
    """
    host = _ls_remote_host("", returncode=128, stderr="no such remote")
    assert mod.main(host=host) == 1
    assert "cannot verify the single-branch premise" in capsys.readouterr().err


def test_remote_branches_parses_ls_remote_output() -> None:
    r"""Tab-separated `<sha>\trefs/heads/<name>` lines; non-head refs ignored."""

    def run(
        argv: Sequence[str], *, timeout: float, **options: object
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            list(argv),
            0,
            stdout="abc123\trefs/heads/main\ndef456\trefs/heads/release/1.2\n999\trefs/tags/v1\n",
            stderr="",
        )

    assert mod.remote_branches(host=fake_host(run)) == ["main", "release/1.2"]


def test_remote_branches_raises_on_git_failure() -> None:
    """A non-zero git exit propagates as RuntimeError, never an empty list."""

    def run(
        argv: Sequence[str], *, timeout: float, **options: object
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            list(argv),
            128,
            stdout="",
            stderr="fatal: 'origin' does not appear to be a git repository",
        )

    with pytest.raises(RuntimeError, match="ls-remote failed"):
        mod.remote_branches(host=fake_host(run))


def test_remote_timeout_cannot_report_a_verified_premise(
    capsys: pytest.CaptureFixture[str],
) -> None:
    def run(
        argv: Sequence[str], *, timeout: float, **options: object
    ) -> subprocess.CompletedProcess[str]:
        assert list(argv) == ["git", "ls-remote", "--heads", "origin"]
        assert "check" not in options
        env = options["env"]
        assert isinstance(env, dict) and env["GIT_TERMINAL_PROMPT"] == "0"
        raise subprocess.TimeoutExpired(list(argv), timeout)

    assert mod.main(host=fake_host(run)) == 1
    error = capsys.readouterr().err
    assert "cannot verify the single-branch premise" in error
    assert "timed out" in error

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from base.host.proc import run_bounded
from base.paths.home import home_checkout_error
from cli.fleet_update.plan.host_commands import HOME, MEIVA


def _home_with_source(tmp_path: Path) -> Path:
    home = tmp_path / ".meiva"
    (home / "source").mkdir(parents=True)
    return home


# meiva:note
# The gate reads the child's own environment; the parent only supplies values at spawn.
def _gate(
    argv: list[str], repo: Path, *, user_home: Path, home: Path | None
) -> tuple[int | None, str]:
    env = {"HOME": str(user_home), "MEIVA_CONFIG_FETCH": "skip", "PATH": "/usr/bin:/bin"}
    if home is not None:
        env["MEIVA_HOME"] = str(home)
    child = run_bounded(
        [
            sys.executable,
            "-I",
            "-B",
            "-m",
            "cli.tests.guards.checkout_guard_child",
            str(repo),
            *argv,
        ],
        cwd=user_home,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert child.returncode == 0, child.stderr
    receipt = json.loads(child.stdout)
    assert receipt["home"] == str(home if home is not None else user_home / ".meiva")
    decision: int | None = receipt["decision"]
    assert decision is None or type(decision) is int
    return decision, child.stderr


def test_a_home_without_source_accepts_any_checkout(tmp_path: Path) -> None:
    assert home_checkout_error(tmp_path / "some-worktree", home=tmp_path) is None


def test_the_homes_own_source_is_accepted(tmp_path: Path) -> None:
    home = _home_with_source(tmp_path)
    assert home_checkout_error(home / "source", home=home) is None


def test_another_checkout_is_refused_with_both_paths_and_the_way_out(tmp_path: Path) -> None:
    home = _home_with_source(tmp_path)
    error = home_checkout_error(tmp_path / "worktrees" / "dev", home=home)
    assert error is not None
    assert str(tmp_path / "worktrees" / "dev") in error
    assert str(home) in error
    assert str(home / "source" / ".venv" / "bin" / "meiva") in error
    assert "set MEIVA_HOME" in error


def test_a_symlinked_spelling_of_the_same_checkout_is_the_same_checkout(tmp_path: Path) -> None:
    home = _home_with_source(tmp_path)
    alias = tmp_path / "alias"
    alias.symlink_to(home / "source")
    assert home_checkout_error(alias, home=home) is None


def test_the_default_home_is_guarded_when_it_has_a_source(tmp_path: Path) -> None:
    home = _home_with_source(tmp_path)
    assert _gate(["status"], home / "source", user_home=tmp_path, home=None) == (None, "")
    decision, error = _gate(["status"], tmp_path / "Meiva", user_home=tmp_path, home=None)
    assert decision == 1
    assert str(home) in error


@pytest.mark.parametrize(
    "argv",
    [
        ["status"],
        ["cluster", "status"],
        ["agents", "ls"],
        ["config", "get", "MEIVA_TIMEZONE"],
        ["mcp", "ls"],
        ["init"],
        ["init", "--serve-gateway", "--machine-name", "probe"],
        ["start"],
        ["start", "--all-services"],
        ["stop"],
        ["restart"],
        ["converge"],
        ["cluster", "destroy"],
        ["cluster", "db-authority", "issue-unit"],
        ["config", "set", "A=B"],
        ["config", "unset", "A"],
        ["some-verb-nobody-has-heard-of"],
        ["start", "--help"],
        ["status", "-h"],
        ["--help", "start"],
        ["-h", "--help"],
        ["config", "unset", "--", "--help"],
    ],
)
def test_gate_refuses_every_command_from_a_foreign_checkout(
    argv: list[str], tmp_path: Path
) -> None:
    home = _home_with_source(tmp_path)
    source = home / "source"
    before = (home.stat().st_mtime_ns, source.stat().st_mtime_ns)
    decision, err = _gate(argv, tmp_path / "dev", user_home=tmp_path, home=home)
    assert decision == 1
    assert str(home / "source" / ".venv" / "bin" / "meiva") in err, "run the home's own CLI"
    assert "set MEIVA_HOME" in err, "or name a home of your own"
    assert sorted(path.name for path in home.iterdir()) == ["source"], "nothing may be written"
    assert list(source.iterdir()) == []
    assert (home.stat().st_mtime_ns, source.stat().st_mtime_ns) == before


def test_gate_does_not_echo_the_argv(tmp_path: Path) -> None:
    home = _home_with_source(tmp_path)
    argv = ["config", "set", "DEEPSEEK_API_KEY=sk-not-for-the-terminal"]
    decision, error = _gate(argv, tmp_path / "dev", user_home=tmp_path, home=home)
    assert decision == 1
    assert "sk-not-for-the-terminal" not in error


@pytest.mark.parametrize("argv", [[], ["--help"], ["-h"]])
def test_gate_passes_bare_meiva_and_a_lone_help_flag(argv: list[str], tmp_path: Path) -> None:
    home = _home_with_source(tmp_path)
    assert _gate(argv, tmp_path / "dev", user_home=tmp_path, home=home) == (None, "")


@pytest.mark.parametrize(
    "argv",
    [["status"], ["stop"], ["start", "--help"], ["config", "set", "A=B"], ["init"], ["start"]],
)
def test_gate_passes_everything_for_the_homes_own_checkout(argv: list[str], tmp_path: Path) -> None:
    home = _home_with_source(tmp_path)
    assert _gate(argv, home / "source", user_home=tmp_path, home=home) == (None, "")


@pytest.mark.parametrize(
    "argv",
    [["status"], ["stop"], ["start", "--help"], ["config", "set", "A=B"], ["init"], ["start"]],
)
def test_gate_passes_everything_for_a_home_without_a_source(
    argv: list[str], tmp_path: Path
) -> None:
    assert _gate(argv, tmp_path / "dev", user_home=tmp_path, home=tmp_path) == (None, "")


def test_the_fleet_updater_runs_the_homes_own_cli_on_every_host() -> None:
    assert 'S="$H/source"' in HOME
    assert MEIVA == 'MEIVA_HOME="$H" "$S/.venv/bin/meiva"'

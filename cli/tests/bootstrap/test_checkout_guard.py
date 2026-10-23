from __future__ import annotations

from pathlib import Path

import pytest

from base.paths.home import home_checkout_error, resolve_meiva_home
from cli import fleet_update, preflight


def _home_with_source(tmp_path: Path) -> Path:
    home = tmp_path / ".meiva"
    (home / "source").mkdir(parents=True)
    return home


# meiva:note
# A home that carries its own source, named by `MEIVA_HOME` as the gate reads it.
def _operated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = _home_with_source(tmp_path)
    monkeypatch.setenv("MEIVA_HOME", str(home))
    return home


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


def test_the_default_home_is_guarded_when_it_has_a_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("MEIVA_HOME", raising=False)
    (tmp_path / ".meiva" / "source").mkdir(parents=True)
    assert home_checkout_error(tmp_path / ".meiva" / "source", home=resolve_meiva_home()) is None
    assert home_checkout_error(tmp_path / "Meiva", home=resolve_meiva_home()) is not None


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
    argv: list[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = _operated_home(tmp_path, monkeypatch)
    assert preflight.require_own_checkout(argv, tmp_path / "dev") == 1
    err = capsys.readouterr().err
    assert str(home / "source" / ".venv" / "bin" / "meiva") in err, "run the home's own CLI"
    assert "set MEIVA_HOME" in err, "or name a home of your own"
    assert sorted(path.name for path in home.iterdir()) == ["source"], "nothing may be written"


def test_gate_does_not_echo_the_argv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _operated_home(tmp_path, monkeypatch)
    argv = ["config", "set", "DEEPSEEK_API_KEY=sk-not-for-the-terminal"]
    assert preflight.require_own_checkout(argv, tmp_path / "dev") == 1
    assert "sk-not-for-the-terminal" not in capsys.readouterr().err


@pytest.mark.parametrize("argv", [[], ["--help"], ["-h"]])
def test_gate_passes_bare_meiva_and_a_lone_help_flag(
    argv: list[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _operated_home(tmp_path, monkeypatch)
    assert preflight.require_own_checkout(argv, tmp_path / "dev") is None


@pytest.mark.parametrize(
    "argv",
    [["status"], ["stop"], ["start", "--help"], ["config", "set", "A=B"], ["init"], ["start"]],
)
def test_gate_passes_everything_for_the_homes_own_checkout(
    argv: list[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _operated_home(tmp_path, monkeypatch)
    assert preflight.require_own_checkout(argv, home / "source") is None


@pytest.mark.parametrize(
    "argv",
    [["status"], ["stop"], ["start", "--help"], ["config", "set", "A=B"], ["init"], ["start"]],
)
def test_gate_passes_everything_for_a_home_without_a_source(
    argv: list[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MEIVA_HOME", str(tmp_path))
    assert preflight.require_own_checkout(argv, tmp_path / "dev") is None


def test_the_fleet_updater_runs_the_homes_own_cli_on_every_host() -> None:
    assert 'S="$H/source"' in fleet_update.HOME
    assert fleet_update.MEIVA == 'MEIVA_HOME="$H" "$S/.venv/bin/meiva"'

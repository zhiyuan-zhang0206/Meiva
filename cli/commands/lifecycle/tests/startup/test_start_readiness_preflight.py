# meiva:note
# The pre-stop start-readiness gate (task #3156; restart caller #3165), against a real home.
#
# `meiva start`'s read-only local checks are moved in front of the stop: a failure HERE refuses the
# stop while the host still serves, instead of failing on a stopped host. What is fatal and what is
# only an observation is decided from facts and asserted in `../start_plans/test_preflight_findings.py`;
# these tests show the shell reads those facts from a real home: private trees, FIFOs and symlinks
# under a temporary directory, a checkout with a real venv layout, and a git repository of tracked
# migrations. `check_launcher` toggles the `.venv` entry-point check that only a caller that execs
# the launcher needs. The caller-level refusal contract lives in the restart tests.

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from base.deploy.schema.tests.migration_support import git, init_repo
from base.host.system.tests.fake_commands import fake_host
from cli.commands.lifecycle.start import readiness_preflight as preflight
from cli.database.runtime import CommandRuntime
from cli.start_runtime import StartRuntime


@pytest.fixture
def home(tmp_path: Path) -> Path:
    """A unit home with the three private trees."""
    home = tmp_path / "home"
    for name in ("logs", "workspaces", "memory"):
        (home / name).mkdir(parents=True)
    return home


def _venv(
    tmp_path: Path,
    *,
    python: bool = True,
    python_executable: bool = True,
    meiva: bool = False,
    meiva_executable: bool = True,
) -> Path:
    """A checkout carrying the requested entry-point layout under `.venv/bin/`."""
    repo = tmp_path / "repo"
    for name, present, executable in (
        ("python", python, python_executable),
        ("meiva", meiva, meiva_executable),
    ):
        if not present:
            continue
        entry = repo / ".venv" / "bin" / name
        entry.parent.mkdir(parents=True, exist_ok=True)
        entry.write_text("#!/bin/sh\n")
        entry.chmod(0o755 if executable else 0o644)
    repo.mkdir(parents=True, exist_ok=True)
    return repo


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """The healthy baseline every non-entry-point test starts from: a checkout whose
    `.venv/bin/python` is executable."""
    return _venv(tmp_path)


@pytest.fixture
def migrations(tmp_path: Path) -> Path:
    """A git worktree of one tracked, readable migration."""
    directory = tmp_path / "migrations"
    directory.mkdir()
    (directory / "20260912T010000_x.sql").write_text("SELECT 1;\n")
    init_repo(directory)
    return directory


def _run(
    repo: Path,
    home: Path,
    migrations: Path,
    command: CommandRuntime,
    *,
    check_launcher: bool = True,
) -> int:
    return preflight.preflight_start_readiness(
        repo,
        home=home,
        check_launcher=check_launcher,
        runtime=StartRuntime(repo, repo, Path(sys.executable).absolute()),
        only_services=(),
        disabled_services=(),
        all_services=True,
        machine=command.machine,
        host=fake_host(),
        migrations_dir=migrations,
    )


# meiva:note
# The gate over one home, checkout and migrations tree.
class Run:
    def __init__(self, repo: Path, home: Path, migrations: Path, command: CommandRuntime) -> None:
        self.repo, self.home, self.migrations, self.command = repo, home, migrations, command

    def __call__(self, *, check_launcher: bool = True, repo: Path | None = None) -> int:
        return _run(
            repo or self.repo,
            self.home,
            self.migrations,
            self.command,
            check_launcher=check_launcher,
        )


@pytest.fixture
def run(repo: Path, home: Path, migrations: Path, command_runtime: CommandRuntime) -> Run:
    return Run(repo, home, migrations, command_runtime)


def test_passes_on_a_clean_home(run: Run, capsys: pytest.CaptureFixture[str]) -> None:
    assert run() == 0
    assert "start-readiness preflight passed" in capsys.readouterr().out


# meiva:note
# An unresolvable identity is the probe gate's refusal, not this one's: the port checks are
# skipped and the update still proceeds.
def test_an_unresolved_machine_role_skips_the_port_checks(
    run: Run, capsys: pytest.CaptureFixture[str]
) -> None:
    for flag in ("machine_serve_gateway", "machine_serve_agent_runner"):
        run.command.config.set_field(flag, None)
    run.command.machine.reset()

    assert run() == 0
    assert "machine role did not resolve" in capsys.readouterr().out


# meiva:note
# `meiva start` refuses a prod home launched from a dev checkout; that refusal must be
# reachable before the stop. A home carrying its own `source` checkout accepts only that.
def test_prod_checkout_violation_is_fatal(run: Run, capsys: pytest.CaptureFixture[str]) -> None:
    (run.home / "source").mkdir()

    assert run() == 1
    assert "is not the checkout of the home it would act on" in capsys.readouterr().err


def test_private_tree_root_symlink_is_fatal(
    run: Run, repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A `logs` root converge would abort on, checked without converging it."""
    (run.home / "logs").rmdir()
    (run.home / "logs").symlink_to(repo)

    assert run() == 1
    err = capsys.readouterr().err
    assert "is a symlink" in err
    assert str(run.home / "logs") in err


def test_private_tree_root_file_is_fatal(run: Run, capsys: pytest.CaptureFixture[str]) -> None:
    (run.home / "workspaces").rmdir()
    (run.home / "workspaces").write_text("not a directory")

    assert run() == 1
    assert "is not a directory" in capsys.readouterr().err


# meiva:note
# The marker converge touches and then repairs: a FIFO there aborts the marker write, so it
# refuses the stop.
def test_metadata_marker_must_be_a_regular_file(
    run: Run, capsys: pytest.CaptureFixture[str]
) -> None:
    os.mkfifo(run.home / "logs" / ".metadata_never_index")

    assert run() == 1
    assert "is not a regular file" in capsys.readouterr().err


# meiva:note
# The 2026-09-12 class: converge skips these, so they cannot fail a start, but they are
# reported before the stop instead of only in the converge log.
def test_non_regular_node_inside_a_tree_is_an_observation(
    run: Run, capsys: pytest.CaptureFixture[str]
) -> None:
    node = run.home / "workspaces" / "leftover.sock"
    os.mkfifo(node)

    assert run() == 0
    out = capsys.readouterr().out
    assert str(node) in out
    assert "skips" in out


# meiva:note
# A preflight must never be the thing that takes an update down: a scan that cannot run is
# reported and the stop proceeds.
def test_a_tree_that_cannot_be_scanned_is_an_observation(
    run: Run, capsys: pytest.CaptureFixture[str]
) -> None:
    tree = run.home / "memory"
    tree.chmod(0o000)
    try:
        assert run() == 0
    finally:
        tree.chmod(0o700)

    assert f"could not scan {tree} for non-regular files" in capsys.readouterr().out


# meiva:note
# The apply-side vet: a tracked migration the applier cannot open would fail
# `meiva start`'s apply on the stopped host.
def test_an_unreadable_tracked_migration_is_fatal(
    run: Run, capsys: pytest.CaptureFixture[str]
) -> None:
    unreadable = run.migrations / "20260912T020000_y.sql"
    unreadable.write_text("SELECT 2;\n")
    git(run.migrations, "add", unreadable.name)
    git(run.migrations, "commit", "-q", "-m", "second")
    unreadable.chmod(0o000)
    try:
        assert run() == 1
    finally:
        unreadable.chmod(0o600)

    err = capsys.readouterr().err
    assert "20260912T020000_y" in err
    assert "not readable" in err


# meiva:note
# An enumeration the loader itself refuses also fails the apply; it must refuse here, not
# surprise the stopped host.
def test_a_migrations_directory_that_cannot_be_enumerated_is_fatal(
    run: Run, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run.migrations = tmp_path / "no-such-migrations"

    assert run() == 1
    assert "cannot be enumerated" in capsys.readouterr().err


# meiva:note
# Converge only mkdirs `configs` / `secrets`, so a file there aborts the start after the stop
# (same class as the tree roots); a symlink TO a directory is tolerated by
# `mkdir(exist_ok=True)` and must not refuse.
def test_configs_and_secrets_must_be_directories(
    run: Run, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (run.home / "configs").write_text("not a directory")

    assert run() == 1
    err = capsys.readouterr().err
    assert str(run.home / "configs") in err
    assert "is not a directory" in err

    (run.home / "configs").unlink()
    (run.home / "secrets").symlink_to(tmp_path, target_is_directory=True)
    assert run() == 0, "mkdir(exist_ok=True) tolerates a symlink to a directory"

    (run.home / "secrets").unlink()
    (run.home / "secrets").symlink_to(tmp_path / "absent")
    assert run() == 1, "a dangling symlink makes mkdir raise"


# meiva:note
# Task #3165 (D4): every service session launches through `.venv/bin/python`; a damaged venv
# is the restart verb's real "stopped and cannot come back" class, and nothing else checks it.
def test_missing_venv_interpreter_is_fatal(
    run: Run, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _venv(tmp_path / "damaged", python=False)

    assert run(repo=repo) == 1
    err = capsys.readouterr().err
    assert str(repo / ".venv" / "bin" / "python") in err
    assert "is missing" in err


def test_venv_interpreter_without_exec_bit_is_fatal(
    run: Run, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _venv(tmp_path / "damaged", python_executable=False)

    assert run(repo=repo) == 1
    assert "is not executable" in capsys.readouterr().err


# meiva:note
# The `.venv/bin/meiva` exec bit is checked when the caller's start execs it (the update
# leg) and skipped for an in-process start (`meiva restart`), whose sessions never touch it:
# refusing a bounce over an entry point it does not use would block a viable restart.
def test_launcher_check_covers_the_meiva_entry_point_only_for_the_update_leg(
    run: Run, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _venv(tmp_path / "launcher", meiva=True, meiva_executable=False)
    launcher = repo / ".venv" / "bin" / "meiva"

    assert run(repo=repo) == 1
    assert "not executable" in capsys.readouterr().err

    assert run(repo=repo, check_launcher=False) == 0

    launcher.chmod(0o755)
    assert run(repo=repo) == 0


# meiva:note
# `check_launcher=False` only drops the `meiva` entry point: the interpreter every session
# launches through is checked for both callers.
def test_missing_interpreter_still_refuses_an_in_process_start(
    run: Run, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _venv(tmp_path / "damaged", python=False, meiva=True)

    assert run(repo=repo, check_launcher=False) == 1
    assert "is missing" in capsys.readouterr().err

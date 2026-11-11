# meiva:note
# A window that keeps the application root (`--keep-shell-sessions`) asks every host, before
# anything stops, whether NEW's start could reuse that root. 2026-11-11: it could not, the `up`
# half's start was refused after every service stopped, and production stayed down until each
# gateway root was stopped by hand. The fake `ssh` answers as NEW's check would.
# meiva:link cli/docs/fleet-update-window/fleet-update-window.md

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from cli.fleet_update.plan.root_generation import root_check_script
from cli.fleet_update.programs import VERIFY_SCRIPTS, program_text
from cli.tests.fleet_update.fleet_update_cluster import (
    Cluster,
    git,
    run_host_shell,
    write_executable,
)

_CHANGED = (
    "changed: requested services change the immutable root generation: root launch inputs; "
    "run meiva stop before starting with changed services or configuration"
)


def _kinds(cluster: Cluster) -> list[str]:
    return [kind for kind, _ in cluster.effects]


@pytest.mark.parametrize("command", ["down", "run"])
def test_a_kept_root_that_new_cannot_reuse_refuses_before_anything_stops(
    env: tuple[Cluster, Callable[..., int]], capsys: pytest.CaptureFixture[str], command: str
) -> None:
    cluster, run = env
    cluster.root_checks["gw"] = (0, _CHANGED)

    assert run(command) == 2

    err = capsys.readouterr().out
    assert "refused: gw: the meiva-root this window keeps cannot serve" in err
    assert "root launch inputs" in err and "--close-shell-sessions" in err
    assert not {"stop", "prestage", "switch", "silence-open", "pre-verify"} & set(_kinds(cluster))


def test_every_host_is_asked_and_every_refusal_is_listed(
    env: tuple[Cluster, Callable[..., int]], capsys: pytest.CaptureFixture[str]
) -> None:
    cluster, run = env
    cluster.root_checks["gw"] = (0, _CHANGED)
    cluster.root_checks["lin"] = (0, _CHANGED)

    assert run("down") == 2

    err = capsys.readouterr().out
    assert "gw: the meiva-root" in err and "lin: the meiva-root" in err
    assert _kinds(cluster).count("root-check") == 3


def test_the_dry_run_reports_the_same_refusal(
    env: tuple[Cluster, Callable[..., int]], capsys: pytest.CaptureFixture[str]
) -> None:
    cluster, run = env
    cluster.root_checks["gw"] = (0, _CHANGED)

    assert run("down", "--dry-run") == 2

    assert "refused: gw: the meiva-root this window keeps" in capsys.readouterr().out
    assert set(_kinds(cluster)) == {"root-check"}


def test_a_root_new_can_reuse_lets_the_window_go_on(
    env: tuple[Cluster, Callable[..., int]],
) -> None:
    cluster, run = env

    assert run("down") == 0

    kinds = _kinds(cluster)
    assert kinds.count("root-check") == 3
    assert max(i for i, k in enumerate(kinds) if k == "root-check") < kinds.index("stop")


def test_a_full_stop_has_no_kept_root_to_check(
    env: tuple[Cluster, Callable[..., int]],
) -> None:
    cluster, run = env
    cluster.root_checks["gw"] = (0, _CHANGED)

    assert run("down", "--close-shell-sessions") == 0

    assert "root-check" not in _kinds(cluster)


def test_a_check_that_could_not_run_warns_and_the_window_goes_on(
    env: tuple[Cluster, Callable[..., int]], capsys: pytest.CaptureFixture[str]
) -> None:
    cluster, run = env
    cluster.root_checks["lin"] = (1, "")

    assert run("down") == 0

    assert "WARNING: lin: could not tell whether its kept meiva-root can serve" in (
        capsys.readouterr().out
    )
    assert "stop" in _kinds(cluster)


_STUB_MAIN = (
    "import os, sys\n"
    "def main(argv, *, home):\n"
    "    print('ARGV=' + '|'.join(argv))\n"
    "    print('CWD=' + os.getcwd())\n"
    "    print('HOME=' + str(home))\n"
    "    print('ROOT_GENERATION=same')\n"
    "    return 0\n"
)


# meiva:note
# A host home whose `source` checkout carries NEW's `live_generation` module as a stub.
def _host(tmp_path: Path) -> tuple[Path, Path, str]:
    home = tmp_path / "host-home"
    source = home / ".meiva" / "source"
    package = source / "cli" / "commands" / "lifecycle"
    package.mkdir(parents=True)
    for directory in (source / "cli", source / "cli" / "commands", package):
        (directory / "__init__.py").write_text("")
    (package / "live_generation.py").write_text(_STUB_MAIN)
    (source / "base" / "paths").mkdir(parents=True)
    (source / "base" / "__init__.py").write_text("")
    (source / "base" / "paths" / "__init__.py").write_text(
        "import os\ndef meiva_home():\n    return os.environ['MEIVA_HOME']\n"
    )
    python = source / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    write_executable(python, f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
    git(source, "init", "-q", "--initial-branch=main")
    git(source, "add", "-A")
    git(source, "commit", "-q", "-m", "new")
    return home, source, git(source, "rev-parse", "HEAD")


def _run_script(home: Path, new: str, names: tuple[str, ...]) -> str:
    script = root_check_script(
        program_text(VERIFY_SCRIPTS / "fleet_pre_root_check.sh"),
        new=new,
        selection="--only-service",
        names=names,
    )
    proc = run_host_shell(["bash", "-c", script], env={**os.environ, "HOME": str(home)}, timeout=60)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


# meiva:note
# The real script text: NEW is checked out in a throwaway worktree, NEW's module runs from it with
# the host's own checkout and the selection as arguments, and the worktree is gone afterwards.
def test_the_host_script_runs_news_check_from_a_throwaway_worktree(tmp_path: Path) -> None:
    home, source, new = _host(tmp_path)

    out = _run_script(home, new, ("pty-sessions", "gate"))

    assert f"ARGV={source}|--only-service|pty-sessions|gate" in out
    assert f"CWD={(home / '.meiva' / 'pre-update-root-check').resolve()}" in out
    assert f"HOME={home / '.meiva'}" in out, "the program is given the host's own home"
    assert "ROOT_GENERATION=same" in out and "ROOT_CHECK_RC=0" in out
    assert not (home / ".meiva" / "pre-update-root-check").exists()
    assert git(source, "worktree", "list").count("\n") == 0, "no worktree is left registered"


def test_the_host_script_says_so_when_new_cannot_be_fetched(tmp_path: Path) -> None:
    home, _source, _new = _host(tmp_path)

    out = _run_script(home, "c" * 40, ("pty-sessions",))

    assert out.startswith("SKIPPED: " + "c" * 40) and "ROOT_CHECK_RC" not in out

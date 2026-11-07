from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path
from typing import cast

import pytest

from cli import main as _main
from cli.database import open_operator_database
from cli.parsers import build_parser


def _top_choices() -> set[str]:
    p = build_parser()
    actions = [a for a in p._actions if a.dest == "cmd"]
    assert actions, "no 'cmd' subparser action found"
    choices = actions[0].choices
    assert choices is not None
    return set(choices)


def _cluster_choices() -> set[str]:
    p = build_parser()
    cmd = next(a for a in p._actions if a.dest == "cmd")
    cluster_p = cast("dict[str, argparse.ArgumentParser]", cmd.choices)["cluster"]
    sub = next(a for a in cluster_p._actions if a.dest == "cluster_cmd")
    return set(cast("dict[str, object]", sub.choices))


def _update_choices() -> set[str]:
    p = build_parser()
    cmd = next(a for a in p._actions if a.dest == "cmd")
    update_p = cast("dict[str, argparse.ArgumentParser]", cmd.choices)["update"]
    sub = next(a for a in update_p._actions if a.dest == "half")
    return set(cast("dict[str, object]", sub.choices))


def test_core_verbs_exist() -> None:
    assert {"start", "stop", "status", "pty", "cluster", "update"} <= _top_choices()


def test_update_group_has_run_down_and_up() -> None:
    assert _update_choices() == {"run", "down", "up"}


def test_update_binds_window_inputs_without_touching_local_home(tmp_path: Path) -> None:
    captured: dict[str, argparse.Namespace] = {}

    def _fake(args: argparse.Namespace) -> int:
        captured["args"] = args
        return 0

    argv = ["update", "up", "--gateway", "gw", "--log-dir", str(tmp_path / "logs")]
    args = _main.parse_command_args(argv)
    args.func = _fake
    declared = tmp_path / "untouched-home"
    checkout = Path(_main.__file__).resolve().parents[1]
    assert (
        _main.dispatch_parsed(
            args,
            argv,
            declared=declared,
            checkout=checkout,
            producer_factory=_main.operator_producer,
            open_database=open_operator_database,
        )
        == 0
    )
    assert not declared.exists()
    inputs = captured["args"].update_inputs
    assert inputs.repo == checkout
    assert inputs.python == sys.executable
    assert inputs.operation_id
    assert inputs.initiator.startswith("user:")


def test_cluster_group_has_status_and_destroy() -> None:
    assert {"status", "destroy"} <= _cluster_choices()


def test_cluster_destroy_acts_on_this_home_and_takes_no_path(tmp_path: Path) -> None:
    p = build_parser()
    destroy = ["cluster", "destroy", "--stop-mode", "smooth", "--stop-timeout", "300"]
    assert p.parse_args(destroy).drop_db is False
    assert p.parse_args([*destroy, "--drop-db"]).drop_db is True
    with pytest.raises(SystemExit):
        p.parse_args([*destroy, "--path", str(tmp_path / ".meiva-t")])
    with pytest.raises(SystemExit):
        # meiva:note
        # the stop's mode and budget are explicit
        p.parse_args(["cluster", "destroy"])


def test_cluster_destroy_has_no_flag_that_skips_its_confirmation() -> None:
    p = build_parser()
    for flag in ("--yes", "-y", "--force"):
        with pytest.raises(SystemExit):
            p.parse_args(
                ["cluster", "destroy", "--stop-mode", "smooth", "--stop-timeout", "300", flag]
            )


def test_help_builds_parser_config_free(tmp_path: Path) -> None:
    env = {k: v for k, v in os.environ.items() if not k.startswith("MEIVA_")}
    env["MEIVA_HOME"] = str(tmp_path)
    proc = subprocess.run(
        [sys.executable, "-m", "cli.main", "--help"],
        cwd=Path(__file__).resolve().parents[2],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, f"`meiva --help` failed config-free:\n{proc.stderr}"
    assert proc.stdout.startswith("usage: meiva")

"""Settings-free syntax and command-option contracts for CLI consumers."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from cli.parsers import build_parser, command_options, parse_args


@pytest.mark.parametrize(
    "argv",
    [
        ["memory", "search", "text"],
        ["cluster", "destroy", "--stop-mode", "smooth", "--stop-timeout", "300"],
        ["agents", "timeline", "7"],
    ],
)
def test_parsing_keeps_defaults_and_bound_handlers(argv: list[str]) -> None:
    assert vars(parse_args(argv)) == vars(build_parser().parse_args(argv))


@pytest.mark.parametrize(
    ("argv", "code"),
    [([], 2), (["unknown-command"], 2), (["--help"], 0), (["logs", "retention", "--help"], 0)],
)
def test_help_and_errors_keep_argparse_output(
    argv: list[str], code: int, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as original:
        build_parser().parse_args(argv)
    expected = capsys.readouterr()

    with pytest.raises(SystemExit) as public:
        parse_args(argv)

    assert public.value.code == original.value.code == code
    assert capsys.readouterr() == expected


# meiva:note
# Runs the repository-owned child helper with a fixed interpreter and argv so
# the parser reads a real process `sys.argv` without mutating the parent's.
def test_omitted_arguments_read_the_process_argv() -> None:
    child = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-I",
            str(Path(__file__).with_name("child_public_queries.py")),
            "memory",
            "search",
            "text",
            "--limit",
            "8",
        ],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert child.returncode == 0, child.stderr


def test_options_keep_command_paths_and_fresh_snapshots() -> None:
    options = command_options()
    assert options[("meiva",)] == {"--help"}
    assert "--keep-infra" in options[("meiva", "stop")]
    assert "--keep-infra" not in options[("meiva", "start")]
    timeline = ("meiva", "agents", "timeline")
    fresh_options = command_options()
    assert options[timeline] == fresh_options[timeline] == {"--help", "--limit", "--before"}
    options[timeline].clear()
    assert "--limit" in fresh_options[timeline]
    assert "--limit" in command_options()[timeline]


def test_public_queries_do_not_load_settings_or_dispatch(tmp_path: Path) -> None:
    env = {key: value for key, value in os.environ.items() if not key.startswith("MEIVA_")}
    env["MEIVA_HOME"] = str(tmp_path)
    child = subprocess.run(  # noqa: S603 -- fixed interpreter and repository-owned helper
        [
            sys.executable,
            "-I",
            str(Path(__file__).with_name("child_public_queries.py")),
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert child.returncode == 0, child.stderr

# meiva:note
# The helper's decisions as values: the command line, the usage verdict and the redirect table.
# meiva:link base/native_process/reparent/plan.py

from __future__ import annotations

import os

from base.native_process.reparent.plan import parse_argv, redirect_plan


def test_a_full_line_names_the_program_and_its_arguments() -> None:
    args = parse_argv(["/logs/out", "/logs/err", "/venv/bin/agent", "--foreground", "7"])

    assert args is not None
    assert (args.stdout_log, args.stderr_log) == ("/logs/out", "/logs/err")
    assert args.command == ("/venv/bin/agent", "--foreground", "7")


def test_a_line_too_short_to_name_a_program_has_no_arguments() -> None:
    assert parse_argv(["/logs/out", "/logs/err"]) is None
    assert parse_argv([]) is None


def test_the_two_streams_share_one_file_when_the_paths_match() -> None:
    args = parse_argv(["/logs/session.log", "/logs/session.log", "/bin/true"])

    assert args is not None
    table = redirect_plan(args)

    assert table.stdin_device == os.devnull
    assert table.stdout_log == "/logs/session.log"
    assert table.stderr_log is None


def test_the_two_streams_open_their_own_files_when_the_paths_differ() -> None:
    args = parse_argv(["/logs/out", "/logs/err", "/bin/true"])

    assert args is not None
    table = redirect_plan(args)

    assert (table.stdout_log, table.stderr_log) == ("/logs/out", "/logs/err")

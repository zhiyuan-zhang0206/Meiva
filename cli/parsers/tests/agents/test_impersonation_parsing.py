# meiva:note
# What `meiva impersonate` refuses or accepts before any command code runs: the relay options
# that name no destination, and the numeric options outside the bounds the service validates.

from __future__ import annotations

from argparse import Namespace

import pytest

from cli.database.runtime import CommandRuntime
from cli.database.tests.command_inputs import command_runtime as command_runtime
from cli.parsers import build_parser


def _args(*args: str, command_runtime: CommandRuntime) -> Namespace:
    parsed = build_parser().parse_args(["impersonate", *args])
    parsed.runtime = command_runtime
    return parsed


def test_request_relay_spec_is_checked_at_the_cli_boundary(
    capsys: pytest.CaptureFixture[str], *, command_runtime: CommandRuntime
) -> None:
    base = ["--name", "Fix login", "--as", "codex", "--ttl", "600", "--batch-window", "0"]

    def refusal(*tail: str) -> str:
        args = _args("request", "--agent", "405", *base, *tail, command_runtime=command_runtime)
        assert args.func(args) == 2
        return capsys.readouterr().err

    assert "needs --thread-id" in refusal("--provider", "codex")
    assert "unix:// or ws://" in refusal(
        "--provider", "codex", "--thread-id", "t", "--codex-remote", "http://bad"
    )
    assert "routes to its owner" in refusal("--provider", "claude", "--thread-id", "t")
    assert "the dsh relay routes to its owner" in refusal(
        "--provider", "dsh", "--codex-remote", "unix:///tmp/x.sock"
    )


def test_relay_spec_is_checked_at_the_cli_boundary(
    capsys: pytest.CaptureFixture[str], *, command_runtime: CommandRuntime
) -> None:
    without_thread = _args(
        "relay",
        "405",
        "--lease-id",
        "lease",
        "--provider",
        "codex",
        command_runtime=command_runtime,
    )
    assert without_thread.func(without_thread) == 2
    assert "needs --thread-id" in capsys.readouterr().err

    with_remote = _args(
        "relay",
        "405",
        "--lease-id",
        "lease",
        "--provider",
        "claude",
        "--codex-remote",
        "unix:///tmp/x.sock",
        command_runtime=command_runtime,
    )
    assert with_remote.func(with_remote) == 2
    assert "routes to its owner" in capsys.readouterr().err


@pytest.mark.parametrize("remote", [None, "unix:///tmp/meiva-codex.sock"])
def test_relay_parser(remote: str | None, *, command_runtime: CommandRuntime) -> None:
    args = _args(
        "relay",
        "405",
        "--lease-id",
        "lease",
        "--provider",
        "codex",
        "--thread-id",
        "thread",
        *(["--codex-remote", remote] if remote is not None else []),
        command_runtime=command_runtime,
    )
    assert args.provider == "codex"
    assert args.thread_id == "thread"
    assert args.func.__name__ == "_h_impersonate_relay"
    assert args.codex_remote == remote
    assert args.token_stdin is False
    with_stdin = _args(
        "relay",
        "405",
        "--lease-id",
        "lease",
        "--provider",
        "claude",
        "--token-stdin",
        command_runtime=command_runtime,
    )
    assert with_stdin.token_stdin is True


@pytest.mark.parametrize(
    ("command", "option", "invalid_values"),
    [
        (
            [
                "request",
                "--name",
                "Fix login",
                "--agent",
                "405",
                "--as",
                "codex",
                "--provider",
                "codex",
                "--thread-id",
                "t",
            ],
            "--ttl",
            ["0", "86401"],
        ),
        (["renew", "0", "--agent", "405"], "--ttl", ["-1", "86401"]),
        (["inbox", "0", "--agent", "405"], "--limit", ["0", "1001"]),
        (["inbox", "0", "--agent", "405"], "--wait", ["-1", "nan", "inf"]),
        (
            ["relay", "405", "--lease-id", "lease", "--provider", "claude"],
            "--debounce",
            ["-1", "30.1", "nan", "inf"],
        ),
        (
            [
                "request",
                "--name",
                "Fix login",
                "--agent",
                "405",
                "--as",
                "codex",
                "--provider",
                "codex",
                "--thread-id",
                "t",
            ],
            "--batch-window",
            ["-1", "301"],
        ),
    ],
)
def test_numeric_options_reject_out_of_bounds_values_during_parsing(
    command: list[str],
    option: str,
    invalid_values: list[str],
    capsys: pytest.CaptureFixture[str],
    *,
    command_runtime: CommandRuntime,
) -> None:
    for value in invalid_values:
        with pytest.raises(SystemExit) as raised:
            _args(*command, option, value, command_runtime=command_runtime)
        assert raised.value.code == 2
        output = capsys.readouterr()
        assert output.out == ""
        assert f"argument {option}:" in output.err
        assert "Traceback" not in output.err


@pytest.mark.parametrize(
    ("command", "option", "values"),
    [
        (
            [
                "request",
                "--name",
                "Fix login",
                "--agent",
                "405",
                "--as",
                "codex",
                "--provider",
                "codex",
                "--thread-id",
                "t",
                "--batch-window",
                "0",
            ],
            "--ttl",
            ["1", "86400"],
        ),
        (["renew", "0", "--agent", "405"], "--ttl", ["1", "86400"]),
        (["inbox", "0", "--agent", "405"], "--limit", ["1", "1000"]),
        (["inbox", "0", "--agent", "405"], "--wait", ["0", "0.5", "86400"]),
        (
            ["relay", "405", "--lease-id", "lease", "--provider", "claude"],
            "--debounce",
            ["0", "0.5", "30"],
        ),
    ],
)
def test_numeric_options_accept_runtime_boundaries(
    command: list[str], option: str, values: list[str], *, command_runtime: CommandRuntime
) -> None:
    for value in values:
        parsed = _args(*command, option, value, command_runtime=command_runtime)
        assert getattr(parsed, option.removeprefix("--")) == float(value)

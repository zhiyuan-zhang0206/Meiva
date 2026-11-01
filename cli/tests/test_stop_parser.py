# meiva:note
# Lifecycle verbs default and infer nothing: every missing choice is one usage error.

from __future__ import annotations

import argparse
import os
from pathlib import Path

import pytest

from cli.parsers import build_parser
from cli.parsers import host as host_parsers

# meiva:note
# One answer per choice, in the order of STOP_FORMS.
STOP_GROUPS = (
    ["--close-shell-sessions"],
    ["--stop-browser"],
    ["--stop-extras"],
    ["--keep-no-services"],
    ["--mode", "smooth"],
    ["--timeout", "300"],
    ["--yes"],
    ["--no-message"],
)
FULL_STOP = [arg for group in STOP_GROUPS for arg in group]
STOP_FORMS = (
    "--keep-shell-sessions | --close-shell-sessions",
    "--keep-browser | --stop-browser",
    "--keep-extras | --stop-extras",
    "--keep-service NAME [--keep-service NAME ...] | --keep-no-services",
    "--mode smooth|force",
    "--timeout SECONDS",
    "--yes | --prompt",
    "--message TEXT | --no-message",
)


def _parsed(argv: list[str]) -> argparse.Namespace:
    return build_parser().parse_args(argv)


GATEWAY = frozenset({"gateway", "agent-runner"})
RUNNER = frozenset({"agent-runner"})


def test_bare_stop_names_every_missing_choice_at_once(capsys: pytest.CaptureFixture[str]) -> None:
    assert (
        host_parsers.refuse_usage("stop", host_parsers.stop_problems(_parsed(["stop"]), GATEWAY))
        == 2
    )
    err = capsys.readouterr().err
    for form in (*STOP_FORMS, "--keep-infra | --stop-infra (this host serves the gateway)"):
        assert form in err


@pytest.mark.parametrize("index", range(len(STOP_GROUPS)))
def test_each_missing_stop_choice_is_refused(
    index: int, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = [arg for i, group in enumerate(STOP_GROUPS) if i != index for arg in group]
    assert (
        host_parsers.refuse_usage(
            "stop", host_parsers.stop_problems(_parsed(["stop", *argv, "--stop-infra"]), GATEWAY)
        )
        == 2
    )
    assert STOP_FORMS[index] in capsys.readouterr().err


def test_runner_refuses_a_data_plane_choice(capsys: pytest.CaptureFixture[str]) -> None:
    assert (
        host_parsers.refuse_usage(
            "stop",
            host_parsers.stop_problems(_parsed(["stop", *FULL_STOP, "--keep-infra"]), RUNNER),
        )
        == 2
    )
    assert "this host has no local data plane" in capsys.readouterr().err


def test_unreadable_capabilities_refuse_the_stop(capsys: pytest.CaptureFixture[str]) -> None:
    assert (
        host_parsers.refuse_usage(
            "stop", host_parsers.stop_problems(_parsed(["stop", *FULL_STOP, "--stop-infra"]), None)
        )
        == 2
    )
    assert "capabilities are unreadable" in capsys.readouterr().err


def test_a_choice_pair_is_mutually_exclusive() -> None:
    with pytest.raises(SystemExit) as raised:
        build_parser().parse_args(["stop", "--keep-browser", "--stop-browser"])
    assert raised.value.code == 2


def test_bare_restart_names_the_scope_and_every_choice(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert (
        host_parsers.refuse_usage("restart", host_parsers.restart_problems(_parsed(["restart"])))
        == 2
    )
    err = capsys.readouterr().err
    for form in (
        "the scope: --only agent-host | --all-services",
        "--mode smooth|force",
        "--timeout SECONDS",
        "--start-timeout SECONDS",
        "--message TEXT | --no-message",
        "--keep-shell-sessions | --close-shell-sessions",
        "--keep-browser | --restart-browser",
        "--keep-extras | --restart-extras",
    ):
        assert form in err


def test_restart_only_agent_host_takes_no_tree_choice(capsys: pytest.CaptureFixture[str]) -> None:
    argv = ["restart", "--only", "agent-host", "--mode", "smooth", "--timeout", "60"]
    argv += ["--start-timeout", "120", "--no-message"]
    assert (
        host_parsers.refuse_usage(
            "restart", host_parsers.restart_problems(_parsed([*argv, "--keep-browser"]))
        )
        == 2
    )
    assert "--keep-browser | --restart-browser: not part of --only agent-host" in (
        capsys.readouterr().err
    )
    assert (
        host_parsers.refuse_usage(
            "restart", host_parsers.restart_problems(_parsed([*argv, "--all-services"]))
        )
        == 2
    )
    assert "--only agent-host excludes" in capsys.readouterr().err


def test_start_requires_one_selection(capsys: pytest.CaptureFixture[str]) -> None:
    for argv in (["start"], ["start", "--all-services", "--only-service", "gateway"]):
        with pytest.raises(SystemExit) as raised:
            build_parser().parse_args(argv)
        assert raised.value.code == 2
    assert "--all-services" in capsys.readouterr().err


def test_pause_is_not_a_verb(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["pause"])
    assert "invalid choice: 'pause'" in capsys.readouterr().err


def test_complete_answers_leave_no_problem() -> None:
    assert host_parsers.stop_problems(_parsed(["stop", *FULL_STOP, "--stop-infra"]), GATEWAY) == []
    assert host_parsers.stop_problems(_parsed(["stop", *FULL_STOP]), RUNNER) == []
    only = ["restart", "--only", "agent-host", "--mode", "force", "--timeout", "60"]
    only += ["--start-timeout", "120", "--message", "Zhiyuan reloading plugins"]
    assert host_parsers.restart_problems(_parsed(only)) == []
    tree = ["restart", "--only-service", "gateway", "--keep-shell-sessions", "--keep-browser"]
    tree += [
        "--restart-extras",
        "--mode",
        "smooth",
        "--timeout",
        "200",
        "--start-timeout",
        "600",
        "--no-message",
    ]
    assert host_parsers.restart_problems(_parsed(tree)) == []


# meiva:note
# One answer per restart choice over the service tree, each paired with the form its refusal
# names; the test drops exactly one and expects only that one to be named.
RESTART_GROUPS = (
    (["--all-services"], "the scope: --only agent-host | --all-services"),
    (["--close-shell-sessions"], "--keep-shell-sessions | --close-shell-sessions"),
    (["--keep-browser"], "--keep-browser | --restart-browser"),
    (["--keep-extras"], "--keep-extras | --restart-extras"),
    (["--mode", "smooth"], "--mode smooth|force"),
    (["--timeout", "300"], "--timeout SECONDS"),
    (["--start-timeout", "600"], "--start-timeout SECONDS"),
    (["--no-message"], "--message TEXT | --no-message"),
)


@pytest.mark.parametrize("index", range(len(RESTART_GROUPS)))
def test_each_missing_restart_choice_is_refused_alone(index: int) -> None:
    argv = [arg for i, (group, _form) in enumerate(RESTART_GROUPS) if i != index for arg in group]
    problems = host_parsers.restart_problems(_parsed(["restart", *argv]))
    if index == 0:
        assert problems == [
            RESTART_GROUPS[0][1] + " | --only-service NAME ... | --disable-service NAME ..."
        ]
    else:
        assert problems == [RESTART_GROUPS[index][1]]


@pytest.mark.parametrize("dropped", ["--mode", "--timeout", "--start-timeout", "--message"])
def test_each_missing_agent_host_restart_choice_is_refused_alone(dropped: str) -> None:
    groups = {
        "--mode": ["--mode", "force"],
        "--timeout": ["--timeout", "60"],
        "--start-timeout": ["--start-timeout", "120"],
        "--message": ["--no-message"],
    }
    argv = [token for flag, tokens in groups.items() if flag != dropped for token in tokens]
    problems = host_parsers.restart_problems(_parsed(["restart", "--only", "agent-host", *argv]))
    assert len(problems) == 1 and problems[0].startswith(dropped)


@pytest.mark.parametrize("verb", ["stop", "restart"])
def test_blank_or_conflicting_message_choice_is_refused_by_parser(
    verb: str, capsys: pytest.CaptureFixture[str]
) -> None:
    for bad in (
        [verb, "--message", ""],
        [verb, "--message", "  \t "],
        [verb, "--message", "x", "--no-message"],
    ):
        with pytest.raises(SystemExit) as raised:
            build_parser().parse_args(bad)
        assert raised.value.code == 2
    err = capsys.readouterr().err
    assert "--message cannot be empty" in err
    assert "not allowed with argument" in err


class _Machine:
    def __init__(self, roles: frozenset[str] | None) -> None:
        self.roles = roles

    def role(self) -> frozenset[str]:
        from base.cluster.machine import MachineRoleMissing

        if self.roles is None:
            raise MachineRoleMissing("no capabilities recorded")
        return self.roles


class _Config:
    def process_environment(self) -> dict[str, str]:
        return {}


class _Runtime:
    def __init__(self, roles: frozenset[str] | None, home: Path) -> None:
        self.machine = _Machine(roles)
        self.helper = None
        self.config = _Config()
        self.home = home
        # meiva:note
        # The caller block the stop hands the root names the invocation's own pid; the stand-in
        # states one so the handler never reaches for the process it happens to run in.
        self.pid = os.getpid()


@pytest.mark.parametrize(
    ("roles", "extra", "message"),
    [
        (None, ["--stop-infra"], "capabilities are unreadable"),
        (RUNNER, ["--keep-infra"], "this host has no local data plane"),
        (GATEWAY, [], "--keep-infra | --stop-infra (this host serves the gateway)"),
    ],
)
def test_the_stop_handler_refuses_before_any_command_runs(
    roles: frozenset[str] | None,
    extra: list[str],
    message: str,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    args = _parsed(["stop", *FULL_STOP, *extra])
    args.runtime = _Runtime(roles, tmp_path)
    assert host_parsers.h_stop(args) == 2
    assert message in capsys.readouterr().err


def test_the_restart_handler_refuses_before_any_command_runs(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    args = _parsed(["restart", "--all-services", "--keep-browser", "--mode", "smooth"])
    args.runtime = _Runtime(GATEWAY, tmp_path)
    assert host_parsers.h_restart(args, retained_children=[]) == 2
    err = capsys.readouterr().err
    for form in (
        "--timeout SECONDS",
        "--start-timeout SECONDS",
        "--keep-extras | --restart-extras",
    ):
        assert form in err


def _refusing_args(argv: list[str], home: Path) -> argparse.Namespace:
    # meiva:note
    # A dependency factory that fails loudly: a refusal that reached the database
    # would fail this test with the factory's own error instead of a clean status.
    args = _parsed(argv)
    args.runtime = _Runtime(RUNNER, home)

    def _untouched() -> None:
        raise AssertionError("a refusal must not open the database")

    args.database_factory = _untouched
    args.producer = _untouched
    return args


def test_a_contradictory_stop_answers_the_operator_in_one_line(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    # meiva:note
    # The guard refuses before any unit stops; the operator reads one line naming the
    # conflict and both ways out, never a traceback that looks like the command crashed.
    args = _refusing_args(
        [
            "stop",
            "--keep-shell-sessions",
            "--stop-browser",
            "--stop-extras",
            "--keep-no-services",
            "--mode",
            "smooth",
            "--timeout",
            "60",
            "--yes",
            "--no-message",
        ],
        tmp_path,
    )
    assert host_parsers.h_stop(args) == 1
    err = capsys.readouterr().err
    assert "Traceback" not in err
    assert err.startswith("meiva stop: ")
    assert "--keep-shell-sessions" in err
    assert "--keep-extras" in err


def test_a_dedicated_service_kept_by_the_wrong_choice_answers_in_one_line(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    # meiva:note
    # The same layer answers the other operator-input refusal under the stop kernel:
    # keeping shell sessions or the browser through `--keep-service` is not a crash.
    args = _refusing_args(
        [
            "stop",
            "--close-shell-sessions",
            "--stop-browser",
            "--keep-extras",
            "--mode",
            "smooth",
            "--timeout",
            "60",
            "--yes",
            "--no-message",
            "--keep-service",
            "browser",
        ],
        tmp_path,
    )
    assert host_parsers.h_stop(args) == 1
    err = capsys.readouterr().err
    assert "Traceback" not in err
    assert err.startswith("meiva stop: --keep-service ['browser']: choose ")


def test_a_caller_contract_error_still_surfaces_as_a_traceback(tmp_path: Path) -> None:
    # meiva:note
    # Only the operator's own contradictions become one line: a missing caller-owned
    # child list is a programming error, so it keeps raising instead of being dressed
    # as an operator message.
    args = _parsed(
        [
            "restart",
            "--all-services",
            "--close-shell-sessions",
            "--keep-browser",
            "--keep-extras",
            "--mode",
            "smooth",
            "--timeout",
            "60",
            "--start-timeout",
            "120",
            "--no-message",
        ]
    )
    args.runtime = _Runtime(GATEWAY, tmp_path)
    with pytest.raises(ValueError, match="caller-owned child retention"):
        host_parsers.h_restart(args)

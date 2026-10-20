# meiva:note
# Lifecycle verbs default and infer nothing: every missing choice is one usage error.

from __future__ import annotations

import argparse

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
        "--keep-shell-sessions | --close-shell-sessions",
        "--keep-browser | --restart-browser",
        "--keep-extras | --restart-extras",
    ):
        assert form in err


def test_restart_only_agent_host_takes_no_tree_choice(capsys: pytest.CaptureFixture[str]) -> None:
    argv = ["restart", "--only", "agent-host", "--mode", "smooth", "--timeout", "60"]
    argv += ["--start-timeout", "120"]
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
    only += ["--start-timeout", "120"]
    assert host_parsers.restart_problems(_parsed(only)) == []
    tree = ["restart", "--only-service", "gateway", "--keep-shell-sessions", "--keep-browser"]
    tree += ["--restart-extras", "--mode", "smooth", "--timeout", "200", "--start-timeout", "600"]
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


@pytest.mark.parametrize("dropped", ["--mode", "--timeout", "--start-timeout"])
def test_each_missing_agent_host_restart_choice_is_refused_alone(dropped: str) -> None:
    values = {"--mode": "force", "--timeout": "60", "--start-timeout": "120"}
    argv = [token for flag, value in values.items() if flag != dropped for token in (flag, value)]
    problems = host_parsers.restart_problems(_parsed(["restart", "--only", "agent-host", *argv]))
    assert len(problems) == 1 and problems[0].startswith(dropped)


class _Machine:
    def __init__(self, roles: frozenset[str] | None) -> None:
        self.roles = roles

    def role(self) -> frozenset[str]:
        from base.cluster.machine import MachineRoleMissing

        if self.roles is None:
            raise MachineRoleMissing("no capabilities recorded")
        return self.roles


class _Runtime:
    def __init__(self, roles: frozenset[str] | None) -> None:
        self.machine = _Machine(roles)


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
) -> None:
    args = _parsed(["stop", *FULL_STOP, *extra])
    args.runtime = _Runtime(roles)
    assert host_parsers.h_stop(args) == 2
    assert message in capsys.readouterr().err


def test_the_restart_handler_refuses_before_any_command_runs(
    capsys: pytest.CaptureFixture[str],
) -> None:
    args = _parsed(["restart", "--all-services", "--keep-browser", "--mode", "smooth"])
    args.runtime = _Runtime(GATEWAY)
    assert host_parsers.h_restart(args, retained_children=[]) == 2
    err = capsys.readouterr().err
    for form in (
        "--timeout SECONDS",
        "--start-timeout SECONDS",
        "--keep-extras | --restart-extras",
    ):
        assert form in err

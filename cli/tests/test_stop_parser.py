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
        "--keep-shell-sessions | --close-shell-sessions",
        "--keep-browser | --restart-browser",
        "--keep-extras | --restart-extras",
    ):
        assert form in err


def test_restart_only_agent_host_takes_no_tree_choice(capsys: pytest.CaptureFixture[str]) -> None:
    argv = ["restart", "--only", "agent-host", "--mode", "smooth", "--timeout", "60"]
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
    assert host_parsers.restart_problems(_parsed(only)) == []
    tree = ["restart", "--only-service", "gateway", "--keep-shell-sessions", "--keep-browser"]
    tree += ["--restart-extras", "--mode", "smooth", "--timeout", "200"]
    assert host_parsers.restart_problems(_parsed(tree)) == []

# meiva:note
# Which verbs open the loguru sinks a service process has.

from __future__ import annotations

import pytest

from cli.startup.log_sinks import START_SINK, cli_log_name


@pytest.mark.parametrize(
    ("argv", "name"),
    [
        (["start", "--all-services"], START_SINK),
        (["start"], "cli-start"),
        (["restart"], "cli-restart"),
        (["restart", "--mode", "smooth"], "cli-restart"),
        (["lgtm", "on"], "cli-lgtm"),
        (["lgtm", "off"], "cli-lgtm"),
    ],
)
def test_the_verbs_that_bring_a_unit_up_open_a_named_sink(argv: list[str], name: str) -> None:
    assert cli_log_name(argv) == name


@pytest.mark.parametrize(
    "argv",
    [[], ["status"], ["lgtm", "status"], ["lgtm"], ["init"], ["stop"], ["config", "set", "A=B"]],
)
def test_the_verbs_that_print_to_the_terminal_open_none(argv: list[str]) -> None:
    assert cli_log_name(argv) is None

# meiva:note
# Which verbs open the loguru sinks a service process has.

from __future__ import annotations

import pytest

from cli.startup.entry_plan import (
    LITE_VERBS,
    needs_stop_probe,
    needs_transport_key,
    normalize_profile_env,
    select_lite_api_token,
    uses_lite_config,
)
from cli.startup.log_sinks import START_SINK, cli_log_name, pre_dispatch_log_sink


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


def test_pre_dispatch_log_sink_defers_init_and_start() -> None:
    assert pre_dispatch_log_sink(["start", "--all-services"]) is None
    assert pre_dispatch_log_sink(["init"]) is None
    assert pre_dispatch_log_sink(["restart"]) == "cli-restart"
    assert pre_dispatch_log_sink(["lgtm", "on"]) == "cli-lgtm"


def test_entry_plan_lite_config_and_transport_key() -> None:
    assert "stop" in LITE_VERBS and "restart" not in LITE_VERBS
    assert needs_stop_probe(["stop"])
    assert not needs_stop_probe(["stop", "--force"])
    assert not needs_stop_probe(["status"])
    assert uses_lite_config(["status"], unit_stopped=False)
    assert not uses_lite_config(["stop"], unit_stopped=False)
    assert uses_lite_config(["stop"], unit_stopped=True)
    assert uses_lite_config(["stop", "--force"], unit_stopped=False)
    assert not uses_lite_config(["restart"], unit_stopped=True)
    assert needs_transport_key(["init"])
    assert needs_transport_key(["cluster", "db-authority", "install-unit"])
    assert not needs_transport_key(["status"])


def test_entry_plan_profile_and_token_selection() -> None:
    env = {"MEIVA_PROCESS_PROFILE": "agent"}
    normalize_profile_env(env)
    assert env == {"MEIVA_LAUNCHER_PROFILE": "agent"}
    assert (
        select_lite_api_token(
            explicit_bearer=None,
            has_launcher_context=False,
            local_config_source=False,
            capability_bearer="cap-bearer",
        )
        == "cap-bearer"
    )
    assert (
        select_lite_api_token(
            explicit_bearer="explicit",
            has_launcher_context=False,
            local_config_source=False,
            capability_bearer="cap-bearer",
        )
        is None
    )

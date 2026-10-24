# meiva:note
# `meiva agents terminate|kill` renders what the gateway says happened to the agent's shell
# sessions, against a gateway on a loopback port.

from __future__ import annotations

import pytest

from cli.commands.agents import control
from cli.commands.cluster.tests.gateway_server import Reply, access_to, serving

TERMINATE = ("POST", "/api/agents/7/terminate")


@pytest.mark.parametrize(
    ("shell", "message"),
    [
        ({"when": "at_exit", "killed": []}, "shell sessions are killed when it exits"),
        ({"when": "now", "killed": [2, 5]}, "killed 2 shell session(s): 2, 5"),
        ({"when": "now", "killed": []}, "no shell sessions to kill"),
        (None, "shell sessions NOT killed (its runner predates the option)"),
    ],
)
def test_terminate_renders_supported_wire_timing(
    capsys: pytest.CaptureFixture[str], shell: dict[str, object] | None, message: str
) -> None:
    reply = Reply(body={"status": "enqueued", "shell_sessions": shell})
    with serving({TERMINATE: reply}) as gateway:
        assert (
            control.cmd_agents_terminate(access_to(gateway.url), 7, kill_all_shell_sessions=True)
            == 0
        )
    [request] = gateway.requests
    assert request.body == {"force": False, "kill_all_shell_sessions": True}
    assert message in capsys.readouterr().out


@pytest.mark.parametrize("when", ["later", "", None])
def test_terminate_does_not_claim_unknown_cleanup_completed(
    capsys: pytest.CaptureFixture[str], when: object
) -> None:
    reply = Reply(body={"status": "enqueued", "shell_sessions": {"when": when, "killed": [2]}})
    with (
        serving({TERMINATE: reply}) as gateway,
        pytest.raises(ValueError, match="ShellSessionKillTiming"),
    ):
        control.cmd_agents_terminate(access_to(gateway.url), 7, kill_all_shell_sessions=True)
    assert capsys.readouterr().out == ""


def test_a_plain_terminate_asks_only_for_the_graceful_exit(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with serving({TERMINATE: Reply(body={"status": "enqueued"})}) as gateway:
        assert control.cmd_agents_terminate(access_to(gateway.url), 7) == 0
    assert gateway.requests[0].body == {"force": False}
    assert capsys.readouterr().out == "  ✓ agent 7 terminate: enqueued\n"


def test_kill_forces_and_forwards_the_explicit_source(capsys: pytest.CaptureFixture[str]) -> None:
    with serving({TERMINATE: Reply(body={"status": "enqueued"})}) as gateway:
        assert control.cmd_agents_kill(access_to(gateway.url), 7, source="shell:7") == 0
    assert gateway.requests[0].body == {"force": True, "source": "shell:7"}
    assert capsys.readouterr().out == "  ✓ agent 7 kill: enqueued\n"


def test_an_invalid_source_is_a_provenance_error_before_anything_is_sent() -> None:
    with serving({}) as gateway, pytest.raises(control.ProvenanceError, match="Unrecognized"):
        control.cmd_agents_kill(access_to(gateway.url), 7, source="bogus")
    assert gateway.requests == []

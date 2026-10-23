# meiva:note
# Where a remote agent-runner dials a ready gateway, and where the web password lives.

from __future__ import annotations

from pathlib import Path

from cli.commands.lifecycle.start.gateway_hint import gateway_hint_lines

_HOME = Path("/srv/meiva-home")


def test_the_web_login_password_is_located_without_being_printed() -> None:
    lines = gateway_hint_lines(host="10.0.0.5", port=8000, home=_HOME)

    assert lines[0].startswith("\n→ web login password: the MEIVA_CLUSTER_SECRET value in ")
    assert str(_HOME / ".env") in lines[0]


def test_a_loopback_gateway_says_how_to_make_it_reachable_for_runners() -> None:
    lines = gateway_hint_lines(host="127.0.0.1", port=8000, home=_HOME)

    assert len(lines) == 2
    assert lines[1] == (
        "\n→ gateway reachable at http://127.0.0.1:8000 (loopback only — set "
        "MEIVA_MACHINE_HOST to this box's private-network address to enroll remote "
        "agent-runners)"
    )


def test_a_reachable_gateway_prints_the_join_command_for_a_runner() -> None:
    lines = gateway_hint_lines(host="10.0.0.7", port=8123, home=_HOME)

    assert lines[1] == "\n→ gateway reachable at http://10.0.0.7:8123"
    assert lines[2].startswith(
        "  join an agent-runner: meiva init --serve-agent-runner --no-serve-gateway "
        "--gateway-url http://10.0.0.7:8123 --machine-name <name>"
    )
    assert "MEIVA_DB_CAPABILITY_KEY set from a non-echoing prompt" in lines[2]


def test_localhost_by_name_is_loopback_too() -> None:
    assert "loopback only" in gateway_hint_lines(host="localhost", port=1, home=_HOME)[1]

# meiva:note
# The agents-side impersonation commands: the timeline payload, the relay token channels, the
# environment facts a request takes from its shell, and the relay credential stub.

from __future__ import annotations

import io
import json
import stat
from pathlib import Path

import pytest

from cli.commands.agents.impersonation import ImpersonationEnv, relay_token, write_relay_stub
from cli.commands.agents.timeline import cmd_agents_timeline
from cli.commands.cluster.tests.gateway_server import Reply, access_to, serving

NO_ENV = ImpersonationEnv(
    relay_token=None, dsh_relay_stub=None, antigravity_conversation=None, codex_home=None
)


def test_timeline_preserves_existing_payload(capsys: pytest.CaptureFixture[str]) -> None:
    payload = {
        "items": [{"item_id": "0.0", "kind": "system_prompt", "payload": "head"}],
        "msg_count": 100,
        "has_more": True,
    }
    with serving({("GET", "/api/agents/405/timeline"): Reply(body=payload)}) as gateway:
        assert cmd_agents_timeline(access_to(gateway.url), 405) == 0
    [request] = gateway.requests
    # meiva:note
    # No --limit: the gateway's configured default window applies.
    assert request.target == "/api/agents/405/timeline"
    assert request.headers["authorization"] == "Bearer test-cluster-secret"
    assert json.loads(capsys.readouterr().out) == payload


def test_timeline_sends_the_window_it_was_asked_for(capsys: pytest.CaptureFixture[str]) -> None:
    with serving({("GET", "/api/agents/405/timeline"): Reply(body={"items": []})}) as gateway:
        assert cmd_agents_timeline(access_to(gateway.url), 405, 100, "12.0") == 0
    assert gateway.requests[0].target == "/api/agents/405/timeline?limit=100&before=12.0"
    assert json.loads(capsys.readouterr().out) == {"items": []}


def test_relay_token_channels() -> None:
    env = ImpersonationEnv.from_environ({"MEIVA_IMPERSONATION_RELAY_TOKEN": "from-env"})
    assert relay_token(token_stdin=False, stdin=io.StringIO(), env=env) == "from-env"
    with pytest.raises(ValueError, match="MEIVA_IMPERSONATION_RELAY_TOKEN"):
        relay_token(token_stdin=False, stdin=io.StringIO(), env=NO_ENV)

    # meiva:note
    # The accepting runtime pipes the credential: the first stdin line wins over the environment.
    stdin = io.StringIO("from-stdin\r\nsecond line\n")
    assert relay_token(token_stdin=True, stdin=stdin, env=env) == "from-stdin"
    with pytest.raises(ValueError, match="no relay token on stdin"):
        relay_token(token_stdin=True, stdin=io.StringIO(), env=env)


def test_an_empty_variable_is_an_absent_one() -> None:
    env = ImpersonationEnv.from_environ(
        {
            "MEIVA_IMPERSONATION_RELAY_TOKEN": "",
            "DSH_MEIVA_RELAY_STUB": "",
            "ANTIGRAVITY_CONVERSATION_ID": "  \n",
            "CODEX_HOME": "",
        }
    )
    assert env == NO_ENV


def test_the_environment_facts_are_the_ones_the_hosts_export() -> None:
    env = ImpersonationEnv.from_environ(
        {
            "MEIVA_IMPERSONATION_RELAY_TOKEN": "t",
            "DSH_MEIVA_RELAY_STUB": "/run/dsh/s.env",
            "ANTIGRAVITY_CONVERSATION_ID": " 3f9a \n",
            "CODEX_HOME": "/home/u/.codex",
            "PATH": "/usr/bin",
        }
    )
    assert env == ImpersonationEnv("t", "/run/dsh/s.env", "3f9a", "/home/u/.codex")


def test_the_relay_stub_is_private_and_holds_the_three_facts_the_plugin_loads(
    tmp_path: Path,
) -> None:
    stub = tmp_path / "session" / "relay.env"
    credential = "tok-9"
    write_relay_stub(
        stub, agent_id=405, session_id=9, token=credential, interpreter="/venv/bin/python"
    )
    assert stub.read_text() == (
        f"SID=9\nAGENT=405\nMEIVA_IMPERSONATION_RELAY_TOKEN={credential}\n"
        "MEIVA_IMPERSONATION_RELAY_PY=/venv/bin/python\n"
    )
    assert stat.S_IMODE(stub.stat().st_mode) == 0o600


def test_failed_relay_stub_write_removes_partial_file(tmp_path: Path) -> None:
    stub = tmp_path / ".meiva-relay.env"
    unencodable = "tok-\ud800"
    with pytest.raises(UnicodeEncodeError):
        write_relay_stub(stub, agent_id=405, session_id=9, token=unencodable, interpreter=None)
    assert not stub.exists()

# meiva:note
# The relay's delivery methods against the hosts they deliver into: a Codex app server on a unix
# socket, an Antigravity language server on a loopback port, and the stdout a controller session
# reads. Binding and delivering are exercised together, as the relay does.

from __future__ import annotations

import io
import json
from pathlib import Path
from uuid import UUID

import pytest

from base.agents.impersonation.host_transport import AntigravityServer, write_antigravity_endpoint
from cli.commands.agents.impersonation_adapters import (
    AntigravitySend,
    ClaudeStdout,
    CodexSteer,
    DshStdout,
    bind_adapter,
    deliver,
    notifies_terminal,
)
from cli.commands.agents.relay.destination import (
    AntigravityConversation,
    ClaudeSession,
    CodexThread,
    DshSession,
)
from cli.commands.agents.tests.loopback_hosts import (
    CodexHome,
    antigravity_server,
    codex_app_server,
)
from cli.commands.agents.tests.loopback_hosts import codex_home as codex_home

THREAD = UUID("b9d32d0d-bd27-40fc-83e8-692769b21523")
CSRF = "recorded-csrf"
ROTATED = "rotated"
PUSH = "Meiva push with literal $(no-shell) and `no-shell`"


def test_codex_requires_a_control_endpoint_without_queueing(
    codex_home: CodexHome,
) -> None:
    with pytest.raises(RuntimeError, match=r"Steer.*--codex-remote"):
        bind_adapter(
            CodexThread(THREAD, None),
            codex_home=codex_home.home,
            user_home=Path(codex_home.home),
            antigravity_server=None,
            stdout=io.StringIO(),
        )


@pytest.mark.parametrize("remote", ["explicit", "default", "bare-unix"])
def test_codex_emitter_delivers_literal_input_to_the_owning_server(
    remote: str, codex_home: CodexHome
) -> None:
    with codex_app_server(codex_home.socket, [True]) as host:
        recorded = {"explicit": host.endpoint, "default": None, "bare-unix": "unix://"}[remote]
        adapter = bind_adapter(
            CodexThread(THREAD, recorded),
            codex_home=codex_home.home,
            user_home=Path(codex_home.home),
            antigravity_server=None,
            stdout=io.StringIO(),
        )
        assert adapter == CodexSteer(THREAD, host.endpoint)
        deliver(adapter, PUSH)
    assert host.turns == [(str(THREAD), PUSH)]


@pytest.mark.parametrize("failure", ["refused", "unreachable"])
def test_codex_refusal_never_falls_back_to_pending(failure: str, codex_home: CodexHome) -> None:
    adapter = CodexSteer(THREAD, f"unix://{codex_home.socket}")
    if failure == "unreachable":
        with pytest.raises(RuntimeError, match="Steer delivery failed"):
            deliver(adapter, "push")
        return
    refusal = pytest.raises(RuntimeError, match=r"Steer delivery failed.*ActiveTurnNotSteerable")
    with codex_app_server(codex_home.socket, [False]) as host, refusal:
        deliver(adapter, "push")
    assert host.texts == ["push"]


@pytest.mark.parametrize("session", [ClaudeSession(), DshSession()])
def test_controller_session_adapters_need_no_destination_probe(
    session: ClaudeSession | DshSession,
) -> None:
    stream = io.StringIO()
    expected = ClaudeStdout(stream) if isinstance(session, ClaudeSession) else DshStdout(stream)
    assert (
        bind_adapter(
            session,
            codex_home=None,
            user_home=Path("/nonexistent-user-home"),
            antigravity_server=None,
            stdout=stream,
        )
        == expected
    )


@pytest.mark.parametrize("kind", [ClaudeStdout, DshStdout])
def test_controller_adapter_keeps_multiline_unicode_envelopes(
    kind: type[ClaudeStdout] | type[DshStdout],
) -> None:
    stream = io.StringIO()
    message = 'Meiva message agent=42\n[id=7] kind=chat\nquoted "text" and café'
    deliver(kind(stream), message)
    output = stream.getvalue()
    if kind is DshStdout:
        assert len(output.splitlines()) == 1
        assert json.loads(output) == message
    else:
        assert output == message + "\n"


def test_codex_terminal_notice_is_left_to_the_native_host() -> None:
    server = AntigravityServer(address="127.0.0.1:1", csrf_token=CSRF)
    stream = io.StringIO()
    assert notifies_terminal(ClaudeStdout(stream)) is True
    assert notifies_terminal(DshStdout(stream)) is True
    assert notifies_terminal(CodexSteer(THREAD, "unix:///x.sock")) is False
    assert notifies_terminal(AntigravitySend(THREAD, server)) is False


def test_antigravity_readiness_passes_for_a_resolvable_language_server(
    tmp_path: Path,
) -> None:
    server = AntigravityServer(address="127.0.0.1:59901", csrf_token=CSRF)
    record = write_antigravity_endpoint(server, thread_id=str(THREAD), home=tmp_path)
    adapter = bind_adapter(
        AntigravityConversation(THREAD, str(record)),
        codex_home=None,
        user_home=tmp_path,
        antigravity_server=None,
        stdout=io.StringIO(),
    )
    assert adapter == AntigravitySend(THREAD, server)


def test_antigravity_readiness_resolves_the_language_server(
    tmp_path: Path,
) -> None:
    absent = tmp_path / "absent.json"
    with pytest.raises(RuntimeError, match=f"endpoint record is missing at {absent}"):
        bind_adapter(
            AntigravityConversation(THREAD, str(absent)),
            codex_home=None,
            user_home=tmp_path,
            antigravity_server=None,
            stdout=io.StringIO(),
        )


def test_antigravity_destination_resolves_once_and_every_push_reuses_it(
    tmp_path: Path,
) -> None:
    with antigravity_server() as host:
        server = AntigravityServer(address=host.address, csrf_token=CSRF)
        record = write_antigravity_endpoint(server, thread_id=str(THREAD), home=tmp_path)
        adapter = bind_adapter(
            AntigravityConversation(THREAD, str(record)),
            codex_home=None,
            user_home=tmp_path,
            antigravity_server=None,
            stdout=io.StringIO(),
        )
        # meiva:note
        # The record is read once, at binding: replacing it later does not move the relay.
        write_antigravity_endpoint(
            AntigravityServer(address="127.0.0.1:1", csrf_token=ROTATED),
            thread_id=str(THREAD),
            home=tmp_path,
        )
        for push in ("first", "second"):
            deliver(adapter, push)
    assert [(p.path, p.csrf_token, p.body) for p in host.posts] == [
        (
            "/exa.language_server_pb.LanguageServerService/SendAgentMessage",
            CSRF,
            {"recipient": str(THREAD), "content": push},
        )
        for push in ("first", "second")
    ]


def test_an_antigravity_server_that_is_gone_fails_the_push_loudly(unused_tcp_port: int) -> None:
    gone = AntigravityServer(address=f"127.0.0.1:{unused_tcp_port}", csrf_token=CSRF)
    for _ in range(2):
        with pytest.raises(RuntimeError, match="Antigravity SendAgentMessage delivery failed"):
            deliver(AntigravitySend(THREAD, gone), "push")

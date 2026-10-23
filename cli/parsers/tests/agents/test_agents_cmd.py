"""`meiva agents` thin-client commands — each forwards to the right gateway route
and renders the response, verified without a live gateway.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from cli.commands.agents import control as _agents


class _FakeResp:
    def __init__(self, payload: object, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.text = str(payload)

    @property
    def is_success(self) -> bool:
        return 200 <= self.status_code < 300

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("error", request=None, response=None)  # type: ignore[arg-type]

    def json(self) -> object:
        return self._payload


def _agent_row(agent_id: int, status: str, machine: str, label: str | None) -> dict[str, object]:
    """The fields `cmd_agents_ls` reads from an agent directory card."""
    return {
        "agent_id": agent_id,
        "status": status,
        "machine": machine,
        "label": label,
    }


def _transport(
    *,
    get_fn: Callable[..., Any] | None = None,
    post_fn: Callable[..., Any] | None = None,
) -> _agents.AgentsTransport:
    return _agents.AgentsTransport(
        gateway_base=lambda: "http://gw:8000",
        headers=lambda: {"Authorization": "Bearer secret"},
        get=get_fn,
        post=post_fn,
    )


def _post_transport(payload: object) -> tuple[_agents.AgentsTransport, dict[str, object]]:
    seen: dict[str, object] = {}

    def fake_post(url: str, **kwargs: object) -> _FakeResp:
        seen["url"] = url
        seen["json"] = kwargs.get("json")
        seen["headers"] = kwargs.get("headers")
        return _FakeResp(payload)

    return _transport(post_fn=fake_post), seen


def test_agents_ls_renders_rows(capsys: pytest.CaptureFixture[str]) -> None:
    seen: dict[str, object] = {}

    def fake_get(url: str, **kwargs: object) -> _FakeResp:
        seen["url"] = url
        seen["headers"] = kwargs.get("headers")
        seen["params"] = kwargs.get("params")
        return _FakeResp(
            {
                "agents": [
                    {
                        **_agent_row(1, "idling", "runner-a", "alpha"),
                        "workspace": "/runner/local/workspaces/agent-1",
                    },
                    _agent_row(22, "terminated", "runner-long", None),
                ],
                "next_cursor": None,
            }
        )

    assert _agents.cmd_agents_ls(transport=_transport(get_fn=fake_get)) == 0
    assert seen == {
        "url": "http://gw:8000/api/agents",
        "params": {"scope": "live", "query": "", "limit": 100},
        "headers": {"Authorization": "Bearer secret"},
    }
    out = capsys.readouterr().out
    assert out.splitlines() == [
        "id  status      machine      label",
        " 1  idling      runner-a     alpha",
        "22  terminated  runner-long  ",
    ]
    assert "/runner/local/workspaces" not in out


def test_agents_ls_empty(capsys: pytest.CaptureFixture[str]) -> None:
    def fake_get(*_args: object, **_kwargs: object) -> _FakeResp:
        return _FakeResp({"agents": [], "next_cursor": None})

    assert _agents.cmd_agents_ls(transport=_transport(get_fn=fake_get)) == 0
    assert "(no agents)" in capsys.readouterr().out


def test_agents_ls_passes_page_arguments_and_prints_cursor(
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[dict[str, object]] = []

    def fake_get(_url: str, **kwargs: object) -> _FakeResp:
        calls.append(kwargs)
        return _FakeResp(
            {
                "agents": [_agent_row(42, "terminated", "mini", "research")],
                "next_cursor": 42,
            }
        )

    assert (
        _agents.cmd_agents_ls(
            scope="terminated",
            query="research",
            before_id=99,
            limit=1,
            transport=_transport(get_fn=fake_get),
        )
        == 0
    )
    assert len(calls) == 1
    assert calls[0]["params"] == {
        "scope": "terminated",
        "query": "research",
        "before_id": 99,
        "limit": 1,
    }
    assert "repeat with --before-id 42" in capsys.readouterr().out


def test_agents_ls_parser_passes_page_arguments() -> None:
    import argparse

    from cli.commands.agents.parsers import add_agents_parser

    parser = argparse.ArgumentParser()
    add_agents_parser(parser.add_subparsers())
    args = parser.parse_args(
        [
            "agents",
            "ls",
            "--scope",
            "terminated",
            "--query",
            "research",
            "--before-id",
            "42",
            "--limit",
            "5",
        ]
    )
    seen: dict[str, object] = {}

    def fake_get(_url: str, **kwargs: object) -> _FakeResp:
        seen.update(kwargs)
        return _FakeResp({"agents": [], "next_cursor": None})

    args.transport = _transport(get_fn=fake_get)
    assert args.func(args) == 0
    assert seen["params"] == {
        "scope": "terminated",
        "query": "research",
        "before_id": 42,
        "limit": 5,
    }


def test_agents_ls_preserves_http_error_behavior() -> None:
    def unauthorized(_url: str, **_kwargs: object) -> _FakeResp:
        return _FakeResp({"detail": "Unauthorized"}, status_code=401)

    with pytest.raises(httpx.HTTPStatusError):
        _agents.cmd_agents_ls(transport=_transport(get_fn=unauthorized))


def test_agents_cancel_posts_to_cancel_route(capsys: pytest.CaptureFixture[str]) -> None:
    transport, seen = _post_transport({"status": "cancelled"})
    assert _agents.cmd_agents_cancel(5, transport=transport) == 0
    assert seen["url"] == "http://gw:8000/api/agents/5/cancel"
    assert "agent 5 cancel: cancelled" in capsys.readouterr().out


def test_agents_restart_posts(capsys: pytest.CaptureFixture[str]) -> None:
    transport, seen = _post_transport({"status": "enqueued"})
    assert _agents.cmd_agents_restart(9, transport=transport) == 0
    assert seen["url"] == "http://gw:8000/api/agents/9/restart"
    assert seen["json"] is None
    assert "enqueued" in capsys.readouterr().out


def test_agents_restart_posts_config_overlay(capsys: pytest.CaptureFixture[str]) -> None:
    transport, seen = _post_transport({"status": "enqueued"})
    assert _agents.cmd_agents_restart(9, '{"llm_model":"gpt-5.6-sol"}', transport=transport) == 0
    assert seen["url"] == "http://gw:8000/api/agents/9/restart"
    assert seen["json"] == {"config_overlay": {"llm_model": "gpt-5.6-sol"}}
    assert "enqueued" in capsys.readouterr().out


def test_agents_restart_rejects_non_object_config_json(capsys: pytest.CaptureFixture[str]) -> None:
    transport, seen = _post_transport({"status": "enqueued"})
    assert _agents.cmd_agents_restart(9, '["not", "an", "object"]', transport=transport) == 1
    assert "url" not in seen
    assert "config must be a JSON object" in capsys.readouterr().err


def test_agents_terminate_is_graceful(capsys: pytest.CaptureFixture[str]) -> None:
    transport, seen = _post_transport({"status": "enqueued"})
    assert _agents.cmd_agents_terminate(7, transport=transport) == 0
    assert seen["url"] == "http://gw:8000/api/agents/7/terminate"
    assert seen["json"] == {"force": False}
    assert "terminate" in capsys.readouterr().out


def test_agents_terminate_kill_all_shell_sessions_sends_the_option(
    capsys: pytest.CaptureFixture[str],
) -> None:
    transport, seen = _post_transport(
        {"status": "enqueued", "shell_sessions": {"when": "at_exit", "killed": []}},
    )
    assert _agents.cmd_agents_terminate(7, kill_all_shell_sessions=True, transport=transport) == 0
    assert seen["url"] == "http://gw:8000/api/agents/7/terminate"
    assert seen["json"] == {"force": False, "kill_all_shell_sessions": True}
    assert "shell sessions are killed when it exits" in capsys.readouterr().out


def test_agents_kill_kill_all_shell_sessions_sends_the_option(
    capsys: pytest.CaptureFixture[str],
) -> None:
    transport, seen = _post_transport(
        {"status": "enqueued", "shell_sessions": {"when": "now", "killed": [0, 3]}},
    )
    assert _agents.cmd_agents_kill(7, kill_all_shell_sessions=True, transport=transport) == 0
    assert seen["url"] == "http://gw:8000/api/agents/7/terminate"
    assert seen["json"] == {"force": True, "kill_all_shell_sessions": True}
    assert "killed 2 shell session(s): 0, 3" in capsys.readouterr().out


def test_agents_parser_wires_kill_all_shell_sessions() -> None:
    from cli.parsers import build_parser

    seen: dict[str, bool] = {}

    def fake_post(url: str, **kwargs: Any) -> _FakeResp:
        agent_id = url.split("/")[-2]
        body = kwargs["json"]
        seen[agent_id] = bool(body.get("kill_all_shell_sessions"))
        return _FakeResp({"status": "enqueued", "shell_sessions": None})

    transport = _transport(post_fn=fake_post)
    for argv in (
        ["agents", "terminate", "7", "--kill-all-shell-sessions"],
        ["agents", "kill", "8", "--kill-all-shell-sessions"],
        ["agents", "terminate", "9"],
    ):
        args = build_parser().parse_args(argv)
        args.transport = transport
        assert args.func(args) == 0
    assert seen == {"7": True, "8": True, "9": False}


def test_agents_kill_forces(capsys: pytest.CaptureFixture[str]) -> None:
    # Acceptance is asynchronous even when force interruption was requested.
    transport, seen = _post_transport({"status": "enqueued"})
    assert _agents.cmd_agents_kill(7, transport=transport) == 0
    assert seen["url"] == "http://gw:8000/api/agents/7/terminate"
    assert seen["json"] == {"force": True}
    out = capsys.readouterr().out
    assert "kill" in out and "enqueued" in out
    assert "force_killed" not in out


def test_agents_send_posts_content_and_source(capsys: pytest.CaptureFixture[str]) -> None:
    transport, seen = _post_transport({"status": "delivered"})
    assert _agents.cmd_agents_send(5, "build done", "shell:3", transport=transport) == 0
    assert seen["url"] == "http://gw:8000/api/agents/5/messages"
    assert seen["json"] == {"content": "build done", "source": "shell:3"}
    assert "delivered" in capsys.readouterr().out


def test_agents_send_appends_tail_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # --tail-file appends the end of the file (bounded), so the notice carries
    # the command's last output without a follow-up read.
    log = tmp_path / "out.log"
    log.write_text("early stuff\nmiddle\n" + "x" * 5000 + "\nline a\nline b\nFAILED: test_foo\n")
    transport, seen = _post_transport({"status": "delivered"})
    assert _agents.cmd_agents_send(5, "finished", "shell:3", str(log), transport=transport) == 0
    body = seen["json"]
    assert isinstance(body, dict)
    content = body["content"]
    assert isinstance(content, str)
    assert content.startswith("finished")
    assert "Last output" in content
    assert "FAILED: test_foo" in content
    assert "early stuff" not in content  # only the last 3 lines ride along
    assert "line a" in content and "x" * 100 not in content
    assert len(content) < 3000  # bounded by the tail cap


def test_agents_send_missing_tail_file_still_delivers(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The notice is the primary contract: an unreadable --tail-file must not
    # abort the POST — the failure rides inside the delivered message instead.
    transport, seen = _post_transport({"status": "delivered"})
    missing = tmp_path / "gone.log"
    assert _agents.cmd_agents_send(5, "finished", "shell:3", str(missing), transport=transport) == 0
    body = seen["json"]
    assert isinstance(body, dict)
    content = body["content"]
    assert isinstance(content, str)
    assert content.startswith("finished")
    assert "[tail unavailable:" in content


def test_agents_send_skips_empty_tail(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    log = tmp_path / "empty.log"
    log.write_text("   \n")
    transport, seen = _post_transport({"status": "delivered"})
    assert _agents.cmd_agents_send(5, "msg", "shell:3", str(log), transport=transport) == 0
    body = seen["json"]
    assert isinstance(body, dict)
    assert body["content"] == "msg"  # whitespace-only tail adds nothing


def test_agents_send_surfaces_error_body(capsys: pytest.CaptureFixture[str]) -> None:
    # A server-side protocol fence must print its actionable response before
    # raising. Malformed source syntax is now rejected locally before dialing.
    def reject_post(*_a: object, **_k: object) -> _FakeResp:
        return _FakeResp({"detail": "Unrecognized inbound source"}, status_code=422)

    with pytest.raises(httpx.HTTPStatusError):
        _agents.cmd_agents_send(
            5,
            "msg",
            "external_agent:codex",
            transport=_transport(post_fn=reject_post),
        )
    assert "Unrecognized inbound source" in capsys.readouterr().err


# ── deferred-delivery outbox coverage on the send path (task #3769) ──────────


def test_agents_terminate_renders_the_shell_session_kill(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The kill report is rendered; a requested kill with no report (a runner
    that predates the option) is called out; no request stays silent."""
    transport1, _ = _post_transport(
        {"status": "already_terminated", "shell_sessions": {"when": "now", "killed": []}},
    )
    assert _agents.cmd_agents_terminate(7, kill_all_shell_sessions=True, transport=transport1) == 0
    assert "no shell sessions to kill" in capsys.readouterr().out

    transport2, _ = _post_transport({"status": "enqueued"})
    assert _agents.cmd_agents_terminate(7, kill_all_shell_sessions=True, transport=transport2) == 0
    assert "shell sessions NOT killed" in capsys.readouterr().out

    transport3, _ = _post_transport({"status": "enqueued", "shell_sessions": None})
    assert _agents.cmd_agents_terminate(7, transport=transport3) == 0
    assert "shell" not in capsys.readouterr().out


def test_agents_compact_posts_to_the_compact_route(
    capsys: pytest.CaptureFixture[str],
) -> None:
    transport, seen = _post_transport({"inbound_id": 31})
    assert _agents.cmd_agents_compact(7, transport=transport) == 0
    assert seen["url"] == "http://gw:8000/api/agents/7/compact"
    assert "compact requested: inbound 31" in capsys.readouterr().out


def test_agents_compact_parser_dispatches() -> None:
    import argparse

    from cli.commands.agents.parsers import add_agents_parser

    parser = argparse.ArgumentParser()
    add_agents_parser(parser.add_subparsers())
    args = parser.parse_args(["agents", "compact", "7"])
    assert args.agent_id == 7
    assert args.func.__name__ == "_h_agents_compact"

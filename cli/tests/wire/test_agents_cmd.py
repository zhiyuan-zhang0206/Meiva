# meiva:link cli/docs/operator-surfaces/public-client-wire.meiva.okf.md

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

from cli.tests.wire.public_gateway import WIRE_CREDENTIAL, PublicGateway
from cli.tests.wire.public_gateway import wire as wire


def _agent_row(agent_id: int, status: str, machine: str, label: str | None) -> dict[str, object]:
    return {
        "agent_id": agent_id,
        "status": status,
        "machine": machine,
        "label": label,
    }


def test_agents_ls_renders_rows(wire: PublicGateway, capsys: pytest.CaptureFixture[str]) -> None:
    seen = wire.respond(
        "get",
        {
            "agents": [
                {
                    **_agent_row(1, "idling", "runner-a", "alpha"),
                    "workspace": "/runner/local/workspaces/agent-1",
                },
                _agent_row(22, "terminated", "runner-long", None),
            ],
            "next_cursor": None,
        },
    )
    assert wire.run("agents", "ls") == 0
    assert seen == {
        "url": f"{wire.url}/api/agents",
        "params": {"scope": "live", "query": "", "limit": "100"},
        "headers": {"Authorization": f"Bearer {WIRE_CREDENTIAL}"},
        "json": None,
    }
    out = capsys.readouterr().out
    assert out.splitlines() == [
        "id  status      machine      label",
        " 1  idling      runner-a     alpha",
        "22  terminated  runner-long  ",
    ]
    assert "/runner/local/workspaces" not in out


def test_agents_ls_empty(wire: PublicGateway, capsys: pytest.CaptureFixture[str]) -> None:
    wire.respond("get", {"agents": [], "next_cursor": None})
    assert wire.run("agents", "ls") == 0
    assert "(no agents)" in capsys.readouterr().out


def test_agents_ls_passes_page_arguments_and_prints_cursor(
    wire: PublicGateway, capsys: pytest.CaptureFixture[str]
) -> None:
    wire.respond(
        "get", {"agents": [_agent_row(42, "terminated", "mini", "research")], "next_cursor": 42}
    )
    assert (
        wire.run(
            "agents",
            "ls",
            "--scope",
            "terminated",
            "--query",
            "research",
            "--before-id",
            "99",
            "--limit",
            "1",
        )
        == 0
    )
    assert len(wire.requests) == 1
    assert wire.requests[0]["params"] == {
        "scope": "terminated",
        "query": "research",
        "before_id": "99",
        "limit": "1",
    }
    assert "repeat with --before-id 42" in capsys.readouterr().out


def test_agents_ls_parser_passes_page_arguments(wire: PublicGateway) -> None:
    import argparse

    from cli.commands.agents.parsers import add_agents_parser

    argv = [
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
    parser = argparse.ArgumentParser()
    add_agents_parser(parser.add_subparsers())
    args = parser.parse_args(argv)
    assert {
        "scope": args.scope,
        "query": args.query,
        "before_id": args.before_id,
        "limit": args.limit,
    } == {"scope": "terminated", "query": "research", "before_id": 42, "limit": 5}
    wire.respond("get", {"agents": [], "next_cursor": None})
    assert wire.run(*argv) == 0
    assert wire.requests[0]["params"] == {
        "scope": "terminated",
        "query": "research",
        "before_id": "42",
        "limit": "5",
    }


def test_agents_ls_preserves_http_error_behavior(wire: PublicGateway) -> None:
    wire.respond("get", {"detail": "Unauthorized"}, 401)
    assert wire.run("agents", "ls", expect="http", status=401, path="/api/agents") == 0


def test_agents_cancel_posts_to_cancel_route(
    wire: PublicGateway, capsys: pytest.CaptureFixture[str]
) -> None:
    seen = wire.respond("post", {"status": "cancelled"})
    assert wire.run("agents", "cancel", str(5)) == 0
    assert seen["url"] == f"{wire.url}/api/agents/5/cancel"
    assert "agent 5 cancel: cancelled" in capsys.readouterr().out


def test_agents_restart_posts(wire: PublicGateway, capsys: pytest.CaptureFixture[str]) -> None:
    seen = wire.respond("post", {"status": "enqueued"})
    assert wire.run("agents", "restart", str(9), "-m", "restarting") == 0
    assert seen["url"] == f"{wire.url}/api/agents/9/restart"
    assert seen["json"] == {"message": "restarting"}
    assert "enqueued" in capsys.readouterr().out


def test_agents_restart_posts_config_overlay(
    wire: PublicGateway, capsys: pytest.CaptureFixture[str]
) -> None:
    seen = wire.respond("post", {"status": "enqueued"})
    assert (
        wire.run(
            "agents",
            "restart",
            str(9),
            "--config",
            '{"llm_model":"gpt-5.6-sol"}',
            "-m",
            "restarting",
        )
        == 0
    )
    assert seen["url"] == f"{wire.url}/api/agents/9/restart"
    assert seen["json"] == {"config_overlay": {"llm_model": "gpt-5.6-sol"}, "message": "restarting"}
    assert "enqueued" in capsys.readouterr().out


def test_agents_restart_rejects_non_object_config_json(
    wire: PublicGateway, capsys: pytest.CaptureFixture[str]
) -> None:
    seen = wire.respond("post", {"status": "enqueued"})
    assert wire.run(mode="restart-invalid-config") == 1
    assert "url" not in seen
    assert "config must be a JSON object" in capsys.readouterr().err


def test_agents_terminate_is_graceful(
    wire: PublicGateway, capsys: pytest.CaptureFixture[str]
) -> None:
    seen = wire.respond("post", {"status": "enqueued"})
    assert wire.run("agents", "terminate", str(7), "--no-message") == 0
    assert seen["url"] == f"{wire.url}/api/agents/7/terminate"
    assert seen["json"] == {"force": False}
    assert "terminate" in capsys.readouterr().out


def test_agents_terminate_kill_all_shell_sessions_sends_the_option(
    wire: PublicGateway, capsys: pytest.CaptureFixture[str]
) -> None:
    seen = wire.respond(
        "post", {"status": "enqueued", "shell_sessions": {"when": "at_exit", "killed": []}}
    )
    assert wire.run("agents", "terminate", str(7), "--kill-all-shell-sessions", "--no-message") == 0
    assert seen["url"] == f"{wire.url}/api/agents/7/terminate"
    assert seen["json"] == {"force": False, "kill_all_shell_sessions": True}
    assert "shell sessions are killed when it exits" in capsys.readouterr().out


def test_agents_kill_kill_all_shell_sessions_sends_the_option(
    wire: PublicGateway, capsys: pytest.CaptureFixture[str]
) -> None:
    seen = wire.respond(
        "post", {"status": "enqueued", "shell_sessions": {"when": "now", "killed": [0, 3]}}
    )
    assert wire.run("agents", "kill", str(7), "--kill-all-shell-sessions", "--no-message") == 0
    assert seen["url"] == f"{wire.url}/api/agents/7/terminate"
    assert seen["json"] == {"force": True, "kill_all_shell_sessions": True}
    assert "killed 2 shell session(s): 0, 3" in capsys.readouterr().out


def test_agents_parser_wires_kill_all_shell_sessions(wire: PublicGateway) -> None:
    wire.respond("post", {"status": "enqueued"})
    for argv in (
        ["agents", "terminate", "7", "--kill-all-shell-sessions", "--no-message"],
        ["agents", "kill", "8", "--kill-all-shell-sessions", "--no-message"],
        ["agents", "terminate", "9", "--no-message"],
    ):
        assert wire.run(*argv) == 0
    seen = {
        r["url"].split("/")[-2]: r["json"].get("kill_all_shell_sessions", False)
        for r in wire.requests
    }
    assert seen == {"7": True, "8": True, "9": False}
    assert [r["json"]["force"] for r in wire.requests] == [False, True, False]


def test_agents_kill_forces(wire: PublicGateway, capsys: pytest.CaptureFixture[str]) -> None:
    seen = wire.respond("post", {"status": "enqueued"})
    assert wire.run("agents", "kill", str(7), "--no-message") == 0
    assert seen["url"] == f"{wire.url}/api/agents/7/terminate"
    assert seen["json"] == {"force": True}
    out = capsys.readouterr().out
    assert "kill" in out and "enqueued" in out
    assert "force_killed" not in out


def test_agents_lifecycle_verbs_forward_message(wire: PublicGateway) -> None:
    wire.respond("post", {"status": "enqueued"})
    assert wire.run("agents", "restart", "9", "--message", "Zhiyuan switching model") == 0
    assert wire.requests[-1]["json"] == {"message": "Zhiyuan switching model"}

    assert wire.run("agents", "terminate", "9", "--message", "Task #42 complete") == 0
    assert wire.requests[-1]["json"] == {"force": False, "message": "Task #42 complete"}

    assert wire.run("agents", "kill", "9", "--message", "Hung subprocess") == 0
    assert wire.requests[-1]["json"] == {"force": True, "message": "Hung subprocess"}

    wire.respond("post", {"resurrected": True})
    assert wire.run("agents", "resurrect", "9", "--message", "Resume audit") == 0
    assert wire.requests[-1]["json"] == {"prompt": "Resume audit"}


@pytest.mark.parametrize("verb", ["resurrect", "terminate", "kill"])
def test_agents_lifecycle_verbs_require_nonblank_message_choice(
    verb: str, capsys: pytest.CaptureFixture[str]
) -> None:
    from cli.parsers import build_parser

    for bad in (
        ["agents", verb, "9"],
        ["agents", verb, "9", "--message", ""],
        ["agents", verb, "9", "--message", "   "],
        ["agents", verb, "9", "--message", "reason", "--no-message"],
    ):
        with pytest.raises(SystemExit) as raised:
            build_parser().parse_args(bad)
        assert raised.value.code == 2
    err = capsys.readouterr().err
    assert "one of the arguments --message --no-message is required" in err
    assert "--message cannot be empty" in err
    assert "not allowed with argument" in err


def test_agents_restart_requires_nonblank_message(capsys: pytest.CaptureFixture[str]) -> None:
    from cli.parsers import build_parser

    for bad in (
        ["agents", "restart", "9"],
        ["agents", "restart", "9", "-m", ""],
        ["agents", "restart", "9", "-m", "   "],
        ["agents", "restart", "9", "--message", ""],
    ):
        with pytest.raises(SystemExit) as raised:
            build_parser().parse_args(bad)
        assert raised.value.code == 2
    err = capsys.readouterr().err
    assert "the following arguments are required: -m/--message" in err
    assert "--message cannot be empty" in err


def test_agents_send_posts_content_and_source(
    wire: PublicGateway, capsys: pytest.CaptureFixture[str]
) -> None:
    seen = wire.respond("post", {"status": "delivered"})
    assert wire.run("agents", "send", str(5), "build done", "--source", "shell:3") == 0
    assert seen["url"] == f"{wire.url}/api/agents/5/messages"
    assert seen["json"] == {"content": "build done", "source": "shell:3"}
    assert "delivered" in capsys.readouterr().out


def test_agents_send_appends_tail_file(
    wire: PublicGateway, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    log = tmp_path / "out.log"
    log.write_text("early stuff\nmiddle\n" + "x" * 5000 + "\nline a\nline b\nFAILED: test_foo\n")
    seen = wire.respond("post", {"status": "delivered"})
    assert (
        wire.run(
            "agents",
            "send",
            str(5),
            "finished",
            "--source",
            "shell:3",
            "--tail-file",
            str(log),
        )
        == 0
    )
    body = seen["json"]
    assert isinstance(body, dict)
    content = cast(dict[str, object], body)["content"]
    assert isinstance(content, str)
    assert content.startswith("finished")
    assert "Last output" in content
    assert "FAILED: test_foo" in content
    assert "early stuff" not in content
    assert "line a" in content and "x" * 100 not in content
    assert len(content) < 3000


def test_agents_send_missing_tail_file_still_delivers(
    wire: PublicGateway, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    seen = wire.respond("post", {"status": "delivered"})
    missing = tmp_path / "gone.log"
    assert (
        wire.run(
            "agents",
            "send",
            str(5),
            "finished",
            "--source",
            "shell:3",
            "--tail-file",
            str(missing),
        )
        == 0
    )
    body = seen["json"]
    assert isinstance(body, dict)
    content = cast(dict[str, object], body)["content"]
    assert isinstance(content, str)
    assert content.startswith("finished")
    assert "[tail unavailable:" in content


def test_agents_send_skips_empty_tail(
    wire: PublicGateway, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    log = tmp_path / "empty.log"
    log.write_text("   \n")
    seen = wire.respond("post", {"status": "delivered"})
    assert (
        wire.run("agents", "send", str(5), "msg", "--source", "shell:3", "--tail-file", str(log))
        == 0
    )
    body = seen["json"]
    assert isinstance(body, dict)
    assert body["content"] == "msg"


def test_agents_send_surfaces_error_body(
    wire: PublicGateway, capsys: pytest.CaptureFixture[str]
) -> None:
    wire.respond("post", {"detail": "Unrecognized inbound source"}, 422)
    assert (
        wire.run(
            "agents",
            "send",
            "5",
            "msg",
            "--source",
            "external_agent:codex",
            expect="http",
            status=422,
            path="/api/agents/5/messages",
        )
        == 0
    )
    assert "Unrecognized inbound source" in capsys.readouterr().err


def test_agents_terminate_renders_the_shell_session_kill(
    wire: PublicGateway, capsys: pytest.CaptureFixture[str]
) -> None:
    wire.respond(
        "post", {"status": "already_terminated", "shell_sessions": {"when": "now", "killed": []}}
    )
    assert wire.run("agents", "terminate", str(7), "--kill-all-shell-sessions", "--no-message") == 0
    assert "no shell sessions to kill" in capsys.readouterr().out

    wire.respond("post", {"status": "enqueued"})
    assert wire.run("agents", "terminate", str(7), "--kill-all-shell-sessions", "--no-message") == 0
    assert "shell sessions NOT killed" in capsys.readouterr().out

    wire.respond("post", {"status": "enqueued", "shell_sessions": None})
    assert wire.run("agents", "terminate", str(7), "--no-message") == 0
    assert "shell" not in capsys.readouterr().out


def test_agents_compact_posts_to_the_compact_route(
    wire: PublicGateway, capsys: pytest.CaptureFixture[str]
) -> None:
    seen = wire.respond("post", {"inbound_id": 31})
    assert wire.run("agents", "compact", str(7)) == 0
    assert seen["url"] == f"{wire.url}/api/agents/7/compact"
    assert "compact requested: inbound 31" in capsys.readouterr().out


def test_agents_compact_parser_dispatches() -> None:
    import argparse

    from cli.commands.agents.parsers import add_agents_parser

    parser = argparse.ArgumentParser()
    add_agents_parser(parser.add_subparsers())
    args = parser.parse_args(["agents", "compact", "7"])
    assert args.agent_id == 7
    assert args.func.__name__ == "_h_agents_compact"


def test_agents_transfer_posts_and_renders_summary(
    wire: PublicGateway, capsys: pytest.CaptureFixture[str]
) -> None:
    seen = wire.respond(
        "post",
        {
            "agent_id": 2697,
            "from_machine": "macmini",
            "to_machine": "company-mini",
            "agent_status": "idling",
            "files_transferred": 14,
            "bytes_transferred": 4096,
            "killed_shell_sessions": [0, 2],
            "closed_pages": ["dashboard"],
        },
    )
    assert (
        wire.run(
            "agents",
            "transfer",
            "2697",
            "--to",
            "company-mini",
            "--source",
            "user",
            "--message",
            "Move Output owner to company-mini",
        )
        == 0
    )
    assert seen["url"] == f"{wire.url}/api/agents/2697/transfer"
    assert seen["json"] == {
        "to_machine": "company-mini",
        "source": "user",
        "message": "Move Output owner to company-mini",
    }
    out = capsys.readouterr().out
    assert "agent 2697 transfer: macmini -> company-mini (idling, 14 file(s), 4096 B)" in out
    assert "killed 2 shell session(s): 0, 2" in out
    assert "closed 1 page(s)" in out


def test_agents_transfer_no_message_and_error_body(
    wire: PublicGateway, capsys: pytest.CaptureFixture[str]
) -> None:
    seen = wire.respond(
        "post",
        {
            "agent_id": 2697,
            "from_machine": "macmini",
            "to_machine": "company-mini",
            "agent_status": "terminated",
            "files_transferred": 0,
            "bytes_transferred": 0,
            "killed_shell_sessions": [],
            "closed_pages": [],
        },
    )
    assert wire.run("agents", "transfer", "2697", "--to", "company-mini", "--no-message") == 0
    assert seen["json"] == {"to_machine": "company-mini"}
    assert "macmini -> company-mini (terminated, 0 file(s), 0 B)" in capsys.readouterr().out

    wire.respond("post", {"detail": "agent 2697 is currently running on 'macmini'"}, 409)
    assert (
        wire.run(
            "agents",
            "transfer",
            "2697",
            "--to",
            "company-mini",
            "--no-message",
            expect="http",
            status=409,
            path="/api/agents/2697/transfer",
        )
        == 0
    )
    assert "currently running" in capsys.readouterr().err


def test_agents_transfer_parser_requires_target_and_message_choice(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from cli.parsers import build_parser

    for bad in (
        ["agents", "transfer", "2697", "--no-message"],
        ["agents", "transfer", "2697", "--to", "   ", "--no-message"],
        ["agents", "transfer", "2697", "--to", "company-mini"],
        ["agents", "transfer", "2697", "--to", "company-mini", "--message", "  "],
        [
            "agents",
            "transfer",
            "2697",
            "--to",
            "company-mini",
            "--message",
            "ok",
            "--no-message",
        ],
    ):
        with pytest.raises(SystemExit) as raised:
            build_parser().parse_args(bad)
        assert raised.value.code == 2
    err = capsys.readouterr().err
    assert "--to" in err
    assert "one of the arguments --message --no-message is required" in err

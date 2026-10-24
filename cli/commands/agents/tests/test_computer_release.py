# meiva:note
# `meiva computer release` against a computer-use daemon on a unix socket in the invocation's
# home: the daemon answers one line per connection the way the real one does, and each case
# asserts the request it was sent and what the operator was told.

from __future__ import annotations

import json
import socket
import threading
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

import pytest

from base.host.tests.short_tmp import short_tmp as short_tmp
from base.paths import computer_mcp_socket
from cli.commands.agents.computer import cmd_computer_release


# meiva:note
# A daemon that reads one request line, records it and answers with `reply` (None closes the
# connection without answering).
@contextmanager
def daemon(home: Path, reply: bytes | None) -> Generator[list[dict[str, object]]]:
    path = computer_mcp_socket(home)
    path.parent.mkdir(parents=True)
    requests: list[dict[str, object]] = []
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    listener.listen(1)

    def serve() -> None:
        connection, _ = listener.accept()
        with connection:
            requests.append(json.loads(connection.makefile("rb").readline()))
            if reply is not None:
                connection.sendall(reply + b"\n")

    thread = threading.Thread(target=serve)
    thread.start()
    try:
        yield requests
    finally:
        thread.join(timeout=5)
        listener.close()


def _released(holder: object) -> bytes:
    payload = json.dumps({"released": holder})
    return json.dumps({"ok": True, "result": {"content": [{"text": payload}]}}).encode()


def test_the_release_is_forced_and_names_the_agent_that_held_the_screen(
    short_tmp: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with daemon(short_tmp, _released(405)) as requests:
        assert cmd_computer_release(home=short_tmp) == 0
    assert requests == [
        {"id": 1, "method": "call_tool", "tool": "release_control", "args": {"force": True}}
    ]
    assert capsys.readouterr().out == "released the screen from agent 405\n"


def test_a_free_screen_is_reported_as_such(
    short_tmp: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with daemon(short_tmp, _released(None)):
        assert cmd_computer_release(home=short_tmp) == 0
    assert capsys.readouterr().out == "screen was free (no holder)\n"


def test_a_daemon_that_is_not_listening_is_a_one_line_refusal(
    short_tmp: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cmd_computer_release(home=short_tmp) == 1
    err = capsys.readouterr().err
    assert err.startswith(f"computer-mcp daemon not reachable at {computer_mcp_socket(short_tmp)}")


@pytest.mark.parametrize(
    ("reply", "message"),
    [
        (None, "computer-mcp daemon closed the connection"),
        (b"{not json", "computer-mcp daemon returned a corrupted response"),
        (b"[1, 2]", "computer-mcp daemon returned a corrupted response"),
        (
            json.dumps({"ok": False, "error": "no such tool"}).encode(),
            "release failed: no such tool",
        ),
        (json.dumps({"ok": False}).encode(), "release failed: unknown error"),
        (json.dumps({"ok": "yes"}).encode(), "release failed: unknown error"),
        (
            json.dumps({"ok": True, "result": {"content": [{"text": "{}"}]}}).encode(),
            "computer-mcp daemon returned an unexpected release payload",
        ),
        (_released("405"), "computer-mcp daemon returned an unexpected release payload"),
        (_released(True), "computer-mcp daemon returned an unexpected release payload"),
        (
            json.dumps({"ok": True, "result": None}).encode(),
            "computer-mcp daemon returned an unexpected release payload",
        ),
        (
            json.dumps({"ok": True, "result": {"content": [{"text": 1}]}}).encode(),
            "computer-mcp daemon returned an unexpected release payload",
        ),
        (
            json.dumps({"ok": True, "result": {"content": []}}).encode(),
            "computer-mcp daemon returned an unexpected release payload",
        ),
        (
            json.dumps({"ok": True, "result": {"content": [{"text": "not json"}]}}).encode(),
            "computer-mcp daemon returned an unexpected release payload",
        ),
        (
            json.dumps({"ok": True, "result": {"content": [{"text": "[1]"}]}}).encode(),
            "computer-mcp daemon returned an unexpected release payload",
        ),
    ],
)
def test_a_daemon_that_answers_badly_fails_with_what_it_said(
    reply: bytes | None, message: str, short_tmp: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with daemon(short_tmp, reply):
        assert cmd_computer_release(home=short_tmp) == 1
    assert capsys.readouterr().err == message + "\n"

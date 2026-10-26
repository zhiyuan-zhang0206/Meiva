# meiva:note
# meiva-browser-mcp healthcheck probe: an answer is a bool, a transport failure raises.

import contextlib
import json
import socket
import tempfile
import threading
from collections.abc import Generator
from pathlib import Path

import pytest

import services.supervision.healthchecks.browser_mcp as hc
from base.paths import chrome_mcp_socket


@contextlib.contextmanager
def _socket_home(payload: bytes | None = None, *, refused: bool = False) -> Generator[Path]:
    with tempfile.TemporaryDirectory(prefix="meiva-bmcp-", dir="/tmp") as directory:
        home = Path(directory)
        sock_path = chrome_mcp_socket(home, 9222)
        sock_path.parent.mkdir(parents=True, exist_ok=True)
        if payload is None and not refused:
            yield home
            return
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(sock_path))
        server.listen(1)
        if refused:
            server.close()
            yield home
            return

        def _serve() -> None:
            try:
                conn, _ = server.accept()
                with conn:
                    if conn.recv(4096) and payload is not None:
                        conn.sendall(payload)
            except OSError:
                return

        worker = threading.Thread(target=_serve, daemon=True)
        worker.start()
        try:
            yield home
        finally:
            server.close()
            worker.join(timeout=2)


def test_probe_true_when_the_daemon_answers_ok() -> None:
    with _socket_home(b'{"id":0,"ok":true}\n') as home:
        assert hc.browser_mcp_probe(home, port=9222) is True


def test_probe_false_when_the_daemon_answers_not_ok() -> None:
    with _socket_home(b'{"id":0,"ok":false,"error":"busy"}\n') as home:
        assert hc.browser_mcp_probe(home, port=9222) is False


def test_probe_false_when_the_connection_closes_without_an_answer() -> None:
    with _socket_home(b"") as home:
        assert hc.browser_mcp_probe(home, port=9222) is False


@pytest.mark.parametrize(
    ("payload", "refused", "error"),
    [
        (None, True, ConnectionRefusedError),
        (None, False, FileNotFoundError),
        (b"not-json\n", False, json.JSONDecodeError),
    ],
    ids=["refused", "no-socket", "bad-json"],
)
def test_probe_raises_the_transport_failure_for_the_caller_to_judge(
    payload: bytes | None, refused: bool, error: type[Exception]
) -> None:
    with _socket_home(payload, refused=refused) as home, pytest.raises(error):
        hc.browser_mcp_probe(home, port=9222)

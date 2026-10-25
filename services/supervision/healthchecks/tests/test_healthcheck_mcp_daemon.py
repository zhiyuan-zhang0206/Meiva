"""meiva-mcp-daemon healthcheck — a probe returns a bool, never an exception."""

import contextlib
import logging
import socket
import tempfile
import threading
from collections.abc import Generator
from pathlib import Path

import pytest

import services.supervision.healthchecks.mcp_daemon as hc
from base.paths import mcp_daemon_shared_socket


@contextlib.contextmanager
def _socket_home(payload: bytes | None = None, *, refused: bool = False) -> Generator[Path]:
    with tempfile.TemporaryDirectory(prefix="meiva-mcpd-", dir="/tmp") as directory:
        home = Path(directory)
        sock_path = Path(mcp_daemon_shared_socket(home))
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


def test_alive_true_when_probe_answers() -> None:
    with _socket_home(b'{"id":0,"ok":true}\n') as home:
        assert hc.is_alive(home) is True


@pytest.mark.parametrize(
    ("payload", "refused"),
    [
        (None, True),
        (None, False),
        (b"", False),
        (b"not-json\n", False),
    ],
    ids=["refused", "no-socket", "eof", "bad-json"],
)
def test_alive_false_on_expected_probe_failures(payload: bytes | None, refused: bool) -> None:
    """The transport failures that mean "the daemon is down" stay quiet."""
    with _socket_home(payload, refused=refused) as home:
        assert hc.is_alive(home) is False


def test_alive_false_on_any_unexpected_exception(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Any unforeseen probe failure degrades to "dead", with a traceback so
    nothing is swallowed silently."""
    with (
        _socket_home(b"[]\n") as home,
        caplog.at_level(logging.ERROR, logger="services.supervision.healthchecks.mcp_daemon"),
    ):
        assert hc.is_alive(home) is False
    assert any("raised unexpectedly" in r.getMessage() for r in caplog.records)

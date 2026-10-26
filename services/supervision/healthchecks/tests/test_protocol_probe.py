from __future__ import annotations

import contextlib
import socket
import tempfile
import threading
from collections.abc import Generator
from pathlib import Path

import pytest

from base.daemon.health import ProbeVerdict
from base.sessions.pty.tests.pty_service import PtyServiceProcess, service_socket
from base.sessions.pty.tests.pty_service import pty_service as pty_service
from services.supervision.healthchecks import protocol_probe as probe


@contextlib.contextmanager
def unix_server(payload: bytes) -> Generator[Path]:
    with tempfile.TemporaryDirectory(prefix="meiva-ping-", dir="/tmp") as directory:
        path = Path(directory) / "socket"
        server_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server_sock.bind(str(path))
        server_sock.listen(1)

        def _serve() -> None:
            try:
                conn, _ = server_sock.accept()
                with conn:
                    if conn.recv(4096):
                        conn.sendall(payload)
            except OSError:
                return

        worker = threading.Thread(target=_serve, daemon=True)
        worker.start()
        try:
            yield path
        finally:
            server_sock.close()
            worker.join(timeout=2)


def test_unix_pong_needs_no_root_or_native_inspection() -> None:
    assert not hasattr(probe, "psutil")
    assert not hasattr(probe, "root_process")
    with unix_server(b'{"id":0,"ok":true}\n') as path:
        assert probe.ping(path).alive


@pytest.mark.parametrize("payload", [b'{"id":1,"ok":true}\n', b'{"id":0,"ok":false}\n', b""])
def test_unix_invalid_or_closed_ping_is_down(payload: bytes) -> None:
    with unix_server(payload) as path:
        assert not probe.ping(path).alive


def test_missing_unix_endpoint_is_down(tmp_path: Path) -> None:
    assert probe.probe("mcp-daemon", home=tmp_path, browser_cdp_port=9222).verdict.value == "down"


def test_protocol_ping_reads_the_pty_service_as_up(pty_service: PtyServiceProcess) -> None:
    del pty_service
    result = probe.ping(service_socket())
    assert result.verdict is ProbeVerdict.ALIVE, result.detail


def test_unknown_protocol_service_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unsupported protocol probe: unknown-service"):
        probe.probe("unknown-service", home=tmp_path, browser_cdp_port=9222)

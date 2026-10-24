from __future__ import annotations

import socket
from pathlib import Path

import pytest

from base.sessions.exec_host import paths, protocol


def test_service_socket_path_short_and_fallback(tmp_path: Path) -> None:
    short_run = Path("/var") / "run" / "meiva"
    sock = paths.service_socket_path(short_run)
    assert sock == short_run / "exec-host.sock"
    assert paths.lock_path(short_run) == short_run / "exec-host.lock"

    long_run = tmp_path / ("a" * 110) / "run"
    fallback_sock = paths.service_socket_path(long_run)
    assert fallback_sock.parent == paths.fallback_dir()
    assert fallback_sock.suffix == ".sock"
    assert len(str(fallback_sock)) <= paths.SUN_PATH_MAX


def test_protocol_envelopes_and_roundtrip() -> None:
    ok_msg = protocol.ok(1, {"pid": 123})
    assert ok_msg == {"id": 1, "ok": True, "code": protocol.OK, "data": {"pid": 123}, "error": None}
    decoded_ok = protocol.decode_object(protocol.encode(ok_msg).rstrip(b"\n"))
    assert decoded_ok == ok_msg

    err_msg = protocol.err(2, protocol.BAD_REQUEST, "missing field")
    assert err_msg == {
        "id": 2,
        "ok": False,
        "code": protocol.BAD_REQUEST,
        "data": None,
        "error": "missing field",
    }
    assert protocol.decode_object(protocol.encode(err_msg).rstrip(b"\n")) == err_msg

    chunk_msg = protocol.output_chunk(3, "hello\n")
    assert chunk_msg == {"id": 3, "type": "output", "data": "hello\n"}
    assert protocol.decode_object(protocol.encode(chunk_msg).rstrip(b"\n")) == chunk_msg

    with pytest.raises(TypeError, match="not a JSON object"):
        protocol.decode_object(b"[1, 2, 3]")


def test_read_line_reads_frames_and_enforces_limit() -> None:
    s1, s2 = socket.socketpair()
    try:
        payload = protocol.encode(protocol.ok(7, {"returncode": 0}))
        s1.sendall(payload)
        line = protocol.read_line(s2)
        assert line is not None
        assert protocol.decode_object(line) == protocol.ok(7, {"returncode": 0})

        s1.sendall(b"x" * 2048)
        with pytest.raises(ValueError, match="line exceeds 1024 bytes"):
            protocol.read_line(s2, limit=1024)
    finally:
        s1.close()
        s2.close()

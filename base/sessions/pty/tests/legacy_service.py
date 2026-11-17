from __future__ import annotations

import socket
import threading
from collections.abc import Generator
from contextlib import contextmanager

from base.sessions.pty import protocol
from base.sessions.pty.tests.pty_service import service_socket


@contextmanager
def legacy_service(error: str) -> Generator[list[str]]:
    path = service_socket()
    path.parent.mkdir(parents=True, exist_ok=True)
    calls: list[str] = []
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind(str(path))
        listener.listen()
        listener.settimeout(0.1)
        names = {"meiva-agent-7-shell-1", "meiva-agent-7-shell-10", "meiva-agent-8-shell-1"}
        stop = threading.Event()

        def serve() -> None:
            while not stop.is_set():
                try:
                    conn, _ = listener.accept()
                except TimeoutError:
                    continue
                with conn:
                    line = protocol.read_line(conn)
                    assert line is not None
                    req = protocol.decode_object(line)
                    method = str(req["method"])
                    calls.append(method)
                    if method == "fence_agent_shells":
                        reply = protocol.err(req["id"], protocol.BAD_REQUEST, error)
                    elif method == "ping":
                        reply = protocol.ok(req["id"], {"host_generation": "old"})
                    elif method == "list":
                        reply = protocol.ok(
                            req["id"],
                            {
                                "sessions": [
                                    {
                                        "name": name,
                                        "pid": 1,
                                        "create_time": 1.0,
                                        "started_at": 1.0,
                                        "cmd": "sh",
                                        "cwd": "/",
                                        "generation": "old",
                                    }
                                    for name in sorted(names)
                                ]
                            },
                        )
                    elif method == "kill":
                        name = str(req["name"])
                        names.remove(name)
                        reply = protocol.ok(req["id"], {"mode": "forced", "interrupted": False})
                    else:
                        raise AssertionError(method)
                    conn.sendall(protocol.encode(reply))

        thread = threading.Thread(target=serve)
        thread.start()
        try:
            yield calls
        finally:
            stop.set()
            thread.join(timeout=5)
            path.unlink(missing_ok=True)

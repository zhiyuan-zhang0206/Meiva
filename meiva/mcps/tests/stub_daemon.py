# meiva:note
# A real MCP daemon socket for the SDK-side tests. A thread serves JSON lines on a unix socket under a short `/tmp` path (pytest's `tmp_path` can exceed the AF_UNIX path bound), and for each request line plays the steps `respond` returns: raw bytes to send, a float to sleep that many seconds, or `CLOSE` to drop the connection. `requests` is what the daemon received over the wire, in order, so a test asserts on the real exchange. The client under test is the production `RemoteMCPClient` over this socket.
from __future__ import annotations

import json
import shutil
import socketserver
import tempfile
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from contextlib import suppress
from pathlib import Path
from typing import Any

import pytest

CLOSE = object()
type Step = bytes | float | object
type Respond = Callable[[dict[str, Any]], Iterable[Step]]


def line(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload).encode() + b"\n"


def ok_line(request: dict[str, Any], result: object) -> bytes:
    return line({"id": request["id"], "ok": True, "result": result})


def error_line(request: dict[str, Any], error: str) -> bytes:
    return line({"id": request["id"], "ok": False, "error": error})


class StubDaemon:
    def __init__(self, respond: Respond) -> None:
        self._dir = Path(tempfile.mkdtemp(prefix="mcpd", dir="/tmp"))
        self.path = str(self._dir / "d.sock")
        self.requests: list[dict[str, Any]] = []
        self.connections = 0
        stub = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self) -> None:
                stub.connections += 1
                # meiva:note
                # the client dropped the connection mid-script
                with suppress(OSError):
                    while raw := self.rfile.readline():
                        request = json.loads(raw)
                        stub.requests.append(request)
                        for step in respond(request):
                            if step is CLOSE:
                                return
                            if isinstance(step, float):
                                time.sleep(step)
                            elif isinstance(step, bytes):
                                self.wfile.write(step)
                                self.wfile.flush()

        self._server = socketserver.ThreadingUnixStreamServer(self.path, Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        shutil.rmtree(self._dir, ignore_errors=True)


# meiva:note
# Start stub daemons on real unix sockets; every one is closed after the test.
@pytest.fixture
def stub_daemon() -> Iterator[Callable[[Respond], StubDaemon]]:
    made: list[StubDaemon] = []

    def start(respond: Respond) -> StubDaemon:
        daemon = StubDaemon(respond)
        made.append(daemon)
        return daemon

    yield start
    for daemon in made:
        daemon.close()

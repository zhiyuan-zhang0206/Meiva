# meiva:note
# A scripted HTTP server on a loopback port, for the shells that POST to another process.
#
# `serve_post` stands up a real `ThreadingHTTPServer` on port 0 whose POST routes are scripted by
# the test: a route reads the JSON body the client sent and answers with a status and a JSON body,
# and may run the effect the other process would have (a home machine's ops server committing a
# resurrection, an agent host cancelling a turn). The client under test dials it through its real
# transport, so nothing about the client is replaced. A path nobody scripted answers 404, and a
# route that raises fails the test when the block ends, so a bug in the script is never read as
# the client's failure. `closed_port` is the other half: a loopback port nothing listens on.

from __future__ import annotations

import json
import socket
import threading
from collections.abc import Callable, Generator, Mapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

__all__ = ["Posted", "Reply", "Route", "ServedPost", "closed_port", "serve_post"]


# meiva:note
# One scripted answer.
@dataclass(frozen=True)
class Reply:
    body: dict[str, object]
    status: int = 200


# meiva:note
# One POST as the server saw it.
@dataclass(frozen=True)
class Posted:
    path: str
    body: dict[str, object]


# meiva:note
# The running server: its address and what it has been sent so far.
@dataclass
class ServedPost:
    port: int
    posted: list[Posted] = field(default_factory=list[Posted])
    failures: list[BaseException] = field(default_factory=list[BaseException])

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"


Route = Callable[[dict[str, object]], Reply]


# meiva:note
# Serve `routes` (path to the route answering it) until the block ends.
@contextmanager
def serve_post(routes: Mapping[str, Route]) -> Generator[ServedPost]:
    served = ServedPost(port=0)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body: dict[str, object] = json.loads(self.rfile.read(length) or b"{}")
            served.posted.append(Posted(self.path, body))
            route = routes.get(self.path)
            try:
                reply = Reply({}, status=404) if route is None else route(body)
            except BaseException as exc:
                served.failures.append(exc)
                reply = Reply({}, status=500)
            payload = json.dumps(reply.body).encode()
            with suppress(BrokenPipeError, ConnectionResetError):
                self.send_response(reply.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    served.port = server.server_port
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
    thread.start()
    try:
        yield served
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    if served.failures:
        raise served.failures[0]


# meiva:note
# A loopback port that was free a moment ago: a dial of it is refused.
def closed_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]

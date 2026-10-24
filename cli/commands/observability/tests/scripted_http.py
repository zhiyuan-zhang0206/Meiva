# meiva:note
# A scripted HTTP server on a loopback port, for the shells that dial Grafana, Tempo, a release
# mirror or a gateway relay.
#
# `serve` stands up a real `ThreadingHTTPServer` on port 0 whose answers are scripted per path and
# records every request it saw, body included. The client under test dials it through its real
# transport, so nothing about the client is replaced. A path with a sequence of replies answers in
# order and repeats the last; a path nobody scripted answers 404.

from __future__ import annotations

import threading
from collections.abc import Generator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

__all__ = ["Received", "Reply", "Served", "serve"]


# meiva:note
# One scripted answer. `close` drops the connection without a response, the way a mirror that died
# mid-request does.
@dataclass(frozen=True)
class Reply:
    status: int = 200
    body: bytes = b""
    content_type: str = "application/octet-stream"
    close: bool = False


# meiva:note
# One request as the server saw it.
@dataclass(frozen=True)
class Received:
    method: str
    path: str
    headers: Mapping[str, str]
    body: bytes


@dataclass
class Served:
    url: str
    received: list[Received] = field(default_factory=list[Received])


# meiva:note
# Serve `routes` (path to one reply or an ordered sequence of replies) until the block ends.
@contextmanager
def serve(routes: Mapping[str, Reply | Sequence[Reply]]) -> Generator[Served]:
    served = Served(url="")
    cursor: dict[str, int] = {}
    lock = threading.Lock()

    def next_reply(path: str) -> Reply:
        scripted = routes.get(path)
        if scripted is None:
            return Reply(status=404)
        if isinstance(scripted, Reply):
            return scripted
        with lock:
            index = min(cursor.get(path, 0), len(scripted) - 1)
            cursor[path] = cursor.get(path, 0) + 1
        return scripted[index]

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: object) -> None:
            pass

        def _answer(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            path = self.path.partition("?")[0]
            served.received.append(
                Received(self.command, path, {k.lower(): v for k, v in self.headers.items()}, body)
            )
            reply = next_reply(path)
            if reply.close:
                self.close_connection = True
                return
            with suppress(BrokenPipeError, ConnectionResetError):
                self.send_response(reply.status)
                self.send_header("Content-Type", reply.content_type)
                self.send_header("Content-Length", str(len(reply.body)))
                self.end_headers()
                self.wfile.write(reply.body)

        def do_GET(self) -> None:
            self._answer()

        def do_POST(self) -> None:
            self._answer()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    served.url = f"http://127.0.0.1:{server.server_port}"
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
    thread.start()
    try:
        yield served
    finally:
        server.shutdown()
        server.server_close()
        thread.join()

# meiva:note
# The scripted HTTP server of the command tests: a real listener on a loopback port for the shells
# that dial Grafana, Tempo, a release mirror or a gateway.
#
# `serve` stands up a real `ThreadingHTTPServer` on port 0 whose answers are scripted per path (or
# per method and path) and records every request it saw, body included. The client under test dials
# it through its real transport, so nothing about the client is replaced. A route with a sequence
# of replies answers in order and repeats the last; a request nobody scripted gets `otherwise`
# (a 404 unless the caller says what the real server says). A reply can also misbehave the way a
# bad peer does: drop the connection, sit on the request, or drip its body so slowly that only a
# deadline ends the download.

from __future__ import annotations

import threading
import time
from collections.abc import Generator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

__all__ = ["NOT_FOUND", "Drip", "Received", "Reply", "Route", "Routes", "Served", "serve"]

# meiva:note
# A route is a path (any method) or a `(method, path)` pair; the pair wins when both are scripted.
type Route = str | tuple[str, str]
type Scripted = Reply | Sequence[Reply]
type Routes = Mapping[str, Scripted] | Mapping[tuple[str, str], Scripted] | Mapping[Route, Scripted]


# meiva:note
# A body that arrives in pieces: `chunk_size` bytes every `interval_s`. `close_delimited` omits
# the Content-Length so only the close ends the body; `stop_after_s` abandons the stream early;
# `hold_open_s` keeps the connection open after the last byte.
@dataclass(frozen=True)
class Drip:
    chunk_size: int
    interval_s: float
    close_delimited: bool = False
    stop_after_s: float | None = None
    hold_open_s: float = 0.0


# meiva:note
# One scripted answer. `close` drops the connection without a response, the way a mirror that died
# mid-request does; `delay_s` is how long the server sits on the request before answering (a
# client with a shorter timeout has gone by then); `drip` streams the body instead of sending it
# at once.
@dataclass(frozen=True)
class Reply:
    status: int = 200
    body: bytes = b""
    content_type: str = "application/octet-stream"
    close: bool = False
    delay_s: float = 0.0
    drip: Drip | None = None


# meiva:note
# What a server answers for a request nobody scripted.
NOT_FOUND = Reply(status=404)


# meiva:note
# One request as the server saw it: `target` is the full request target (query included) and
# `path` the same without the query.
@dataclass(frozen=True)
class Received:
    method: str
    target: str
    path: str
    headers: Mapping[str, str]
    body: bytes


# meiva:note
# The running server: its base URL, every request answered so far and the number of body bytes the
# last dripped reply had sent.
@dataclass
class Served:
    url: str
    received: list[Received] = field(default_factory=list[Received])
    bytes_sent: int = 0


def _drip_body(handler: BaseHTTPRequestHandler, reply: Reply, drip: Drip, served: Served) -> None:
    started = time.monotonic()
    handler.send_response(reply.status)
    handler.send_header("Content-Type", reply.content_type)
    if drip.close_delimited:
        handler.send_header("Connection", "close")
    else:
        handler.send_header("Content-Length", str(len(reply.body)))
    handler.end_headers()
    sent = 0
    while sent < len(reply.body):
        if drip.stop_after_s is not None and time.monotonic() - started > drip.stop_after_s:
            return
        piece = reply.body[sent : sent + drip.chunk_size]
        handler.wfile.write(piece)
        handler.wfile.flush()
        sent += len(piece)
        served.bytes_sent = sent
        time.sleep(drip.interval_s)
    if drip.hold_open_s:
        time.sleep(drip.hold_open_s)


# meiva:note
# Serve `routes` until the block ends.
@contextmanager
def serve(routes: Routes, *, otherwise: Reply = NOT_FOUND) -> Generator[Served]:
    table: dict[Route, Scripted] = dict(routes.items())
    served = Served(url="")
    cursor: dict[Route, int] = {}
    lock = threading.Lock()

    def next_reply(method: str, path: str) -> Reply:
        key: Route = (method, path) if (method, path) in table else path
        scripted = table.get(key)
        if scripted is None:
            return otherwise
        if isinstance(scripted, Reply):
            return scripted
        with lock:
            index = min(cursor.get(key, 0), len(scripted) - 1)
            cursor[key] = cursor.get(key, 0) + 1
        return scripted[index]

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: object) -> None:
            del format, args

        def _answer(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            path = self.path.partition("?")[0]
            served.received.append(
                Received(
                    self.command,
                    self.path,
                    path,
                    {k.lower(): v for k, v in self.headers.items()},
                    body,
                )
            )
            reply = next_reply(self.command, path)
            if reply.delay_s:
                time.sleep(reply.delay_s)
            if reply.close:
                self.close_connection = True
                return
            with suppress(BrokenPipeError, ConnectionResetError):
                if reply.drip is not None:
                    _drip_body(self, reply, reply.drip, served)
                    return
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

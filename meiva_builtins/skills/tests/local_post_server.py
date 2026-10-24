# meiva:note
# A scripted HTTP server on a loopback port, for the client side of a POST.
#
# `serve_posts` stands up a real `ThreadingHTTPServer` on port 0 that answers the n-th POST
# with the n-th scripted reply and records every request it saw (path, headers, raw body).
# A POST beyond the script is answered 500 so an unexpected extra request fails the test.
# A client under test dials it through its real transport, so nothing about the client is
# replaced.


from __future__ import annotations

import json
import threading
from collections.abc import Generator, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

__all__ = ["RecordedPost", "Reply", "ServedPosts", "json_reply", "serve_posts", "text_reply"]


@dataclass(frozen=True)
class Reply:
    status: int
    body: str
    content_type: str


def json_reply(payload: object, *, status: int = 200) -> Reply:
    return Reply(status=status, body=json.dumps(payload), content_type="application/json")


def text_reply(body: str, *, status: int = 200) -> Reply:
    return Reply(status=status, body=body, content_type="text/plain")


@dataclass(frozen=True)
class RecordedPost:
    path: str
    headers: dict[str, str]
    body: bytes


# meiva:note
# The running server: its base URL and the POSTs it has answered so far.
@dataclass
class ServedPosts:
    url: str
    requests: list[RecordedPost] = field(default_factory=list[RecordedPost])


def _read_body(handler: BaseHTTPRequestHandler) -> bytes:
    if handler.headers.get("Transfer-Encoding", "").lower() == "chunked":
        chunks: list[bytes] = []
        while True:
            size = int(handler.rfile.readline().split(b";")[0].strip() or b"0", 16)
            if size == 0:
                handler.rfile.readline()
                return b"".join(chunks)
            chunks.append(handler.rfile.read(size))
            handler.rfile.readline()
    return handler.rfile.read(int(handler.headers.get("Content-Length", "0")))


# meiva:note
# Serve the scripted `replies` to successive POSTs.
@contextmanager
def serve_posts(replies: Sequence[Reply]) -> Generator[ServedPosts]:
    served = ServedPosts(url="")
    scripted = list(replies)
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def do_POST(self) -> None:
            body = _read_body(self)
            with lock:
                index = len(served.requests)
                served.requests.append(
                    RecordedPost(self.path, {k.lower(): v for k, v in self.headers.items()}, body)
                )
            reply = (
                scripted[index]
                if index < len(scripted)
                else text_reply("unscripted request", status=500)
            )
            payload = reply.body.encode()
            with suppress(BrokenPipeError, ConnectionResetError):
                self.send_response(reply.status)
                self.send_header("Content-Type", reply.content_type)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

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

# meiva:note
# A scripted HTTP server on a loopback port, for the client side of a GET.
#
# `serve_json` stands up a real `ThreadingHTTPServer` on port 0 whose every GET answers
# one scripted status, JSON body and delay, and records the requests it saw. A client under
# test dials it through its real transport, so nothing about the client is replaced.
# `closed_port_url` is the other half: the URL of a loopback port nothing listens on, which
# refuses the connection. `serve_asgi` serves an assembled ASGI application the same way.


from __future__ import annotations

import json
import socket
import threading
import time
from collections.abc import Generator
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import uvicorn
from starlette.types import ASGIApp

__all__ = ["RecordedRequest", "ServedJson", "closed_port_url", "serve_asgi", "serve_json"]


@dataclass(frozen=True)
class RecordedRequest:
    method: str
    path: str
    headers: dict[str, str]


# meiva:note
# The running server: its base URL and the requests it has answered so far.
@dataclass
class ServedJson:
    url: str
    requests: list[RecordedRequest] = field(default_factory=list[RecordedRequest])


# meiva:note
# Serve `body` as JSON with `status` to every GET, after waiting `delay_s`.
@contextmanager
def serve_json(body: object, *, status: int = 200, delay_s: float = 0.0) -> Generator[ServedJson]:
    payload = json.dumps(body).encode()
    served = ServedJson(url="")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def do_GET(self) -> None:
            served.requests.append(
                RecordedRequest("GET", self.path, {k.lower(): v for k, v in self.headers.items()})
            )
            if delay_s:
                time.sleep(delay_s)
            with suppress(BrokenPipeError, ConnectionResetError):
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
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


# meiva:note
# A loopback URL whose port was free a moment ago: a dial of it is refused.
def closed_port_url() -> str:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    return f"http://127.0.0.1:{port}"


# meiva:note
# Serve an assembled ASGI application on a loopback port; yield its base URL.
#
# For a client under test that must dial a real listener whose answers are a real
# application's. The application's own startup has already run (a test client context
# entered by the caller), so this server runs without its own lifespan.
@contextmanager
def serve_asgi(app: ASGIApp) -> Generator[str]:
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=0, lifespan="off", log_level="warning")
    )
    thread = threading.Thread(target=server.run)
    thread.start()
    deadline = time.monotonic() + 10.0
    while not server.started:
        if not thread.is_alive() or time.monotonic() > deadline:
            server.should_exit = True
            thread.join()
            raise RuntimeError("the ASGI test server did not start")
        time.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join()

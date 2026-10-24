# meiva:note
# A runner's `POST /ops` endpoint as a real listener on port 0, registered in `machines`.
#
# The reaper reaches a machine only through `ops.cluster.rpc.dispatch_to_machine`: it reads the
# machine's `gateway_url` from the registry and POSTs an op envelope there. A test serves the
# runner's answer from this listener, so the real registry read, the real HTTP client and the real
# result handling all run. The test asserts on the rows and messages the reaper leaves behind and
# on the envelopes that arrived on the wire.
from __future__ import annotations

import json
import threading
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import psycopg

__all__ = ["Reply", "Runner", "completed", "failed", "refused", "register_url", "runner"]


# meiva:note
# What the listener sends back: an HTTP status and its JSON body (or plain text).
@dataclass(frozen=True)
class Reply:
    http_status: int
    body: dict[str, Any] | str


# meiva:note
# The op ran and produced `result`.
def completed(result: dict[str, Any]) -> Reply:
    return Reply(200, {"status": "completed", "result": result})


# meiva:note
# The op ran and failed.
def failed(result: dict[str, Any]) -> Reply:
    return Reply(200, {"status": "failed", "result": result})


# meiva:note
# A bare HTTP status. The dispatcher treats a 4xx as "unreachable" at once; a 5xx would be retried
# with a delay, so a test that needs "unreachable" uses a 4xx.
def refused(http_status: int, text: str = "") -> Reply:
    return Reply(http_status, text)


@dataclass
class Runner:
    name: str
    url: str
    calls: list[dict[str, Any]] = field(default_factory=list)


# meiva:note
# Write the `machines` row the dispatcher reads for `name` and commit it. `url=None` is a machine
# registered without an address.
def register_url(conn: psycopg.Connection, name: str, url: str | None) -> None:
    conn.execute(
        "INSERT INTO machines (name, role, gateway_url) VALUES (%s, %s, %s) "
        "ON CONFLICT (name) DO UPDATE SET gateway_url = EXCLUDED.gateway_url",
        (name, ["agent-runner"], url),
    )
    conn.commit()


class _Server(ThreadingHTTPServer):
    # meiva:note
    # A caller that gave up on a slow answer closes the socket; the late write is not an error.
    def handle_error(self, request: object, client_address: object) -> None:
        pass


def _handler(
    served: Runner, answer: Callable[[dict[str, Any]], Reply]
) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", 0))
            envelope: dict[str, Any] = json.loads(self.rfile.read(length) or b"{}")
            served.calls.append(envelope)
            reply = answer(envelope)
            data = (
                reply.body.encode()
                if isinstance(reply.body, str)
                else json.dumps(reply.body).encode()
            )
            self.send_response(reply.http_status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    return Handler


# meiva:note
# Serve `answer` (a function of the arriving envelope) as machine `name` and register it.
@contextmanager
def runner(
    conn: psycopg.Connection, name: str, answer: Callable[[dict[str, Any]], Reply]
) -> Generator[Runner]:
    served = Runner(name=name, url="")
    server = _Server(("127.0.0.1", 0), _handler(served, answer))
    server.daemon_threads = True
    served.url = f"http://127.0.0.1:{server.server_port}"
    serving = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
    serving.start()
    try:
        register_url(conn, name, served.url)
        yield served
    finally:
        server.shutdown()
        server.server_close()
        serving.join()

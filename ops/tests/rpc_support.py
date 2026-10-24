# meiva:note
# A runner's `POST /ops` endpoint as a real listener on port 0, answering from a script.
#
# The RPC client is tested against a real HTTP server on loopback with the real `httpx` client,
# so the transport errors it classifies (a refused connection, a connection closed without an
# answer, a 5xx, a malformed body) are the ones the wire produces. The server answers its replies
# in order and repeats the last one when the script runs out, so a retry loop sees the same
# failure again. `dropped()` reads the request and closes the connection without answering: the
# op reached the runner and the response was lost, which is the failure a retry must survive
# with a stable idempotency key.

from __future__ import annotations

import json
import socket
import threading
from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import cast

from base.clock.tests.manual_timers import ManualTimers
from base.cluster.machine import GatewayAccess
from ops.cluster.rpc import RpcClient, RpcConfig

__all__ = [
    "RPC_TEST_CREDENTIAL",
    "OpsServer",
    "Received",
    "Reply",
    "completed",
    "dropped",
    "envelope",
    "failed",
    "http_reply",
    "rpc_access",
    "rpc_client",
    "scripted_ops_server",
    "unreachable_url",
]

RPC_TEST_CREDENTIAL = "rpc-test"


# meiva:note
# One answer the server can give: an HTTP status and body, or, with `drop`, no answer at all.
@dataclass(frozen=True)
class Reply:
    status: int = 200
    body: bytes = b""
    drop: bool = False


# meiva:note
# One request that reached the server.
@dataclass(frozen=True)
class Received:
    path: str
    authorization: str | None
    body: dict[str, object]


def envelope(status: str, result: dict[str, object]) -> Reply:
    return Reply(body=json.dumps({"status": status, "result": result}).encode())


def completed(result: dict[str, object]) -> Reply:
    return envelope("completed", result)


def failed(result: dict[str, object]) -> Reply:
    return envelope("failed", result)


def http_reply(status: int, text: str = "") -> Reply:
    return Reply(status=status, body=text.encode())


def dropped() -> Reply:
    return Reply(drop=True)


# meiva:note
# A running scripted server. `received` is every request in arrival order.
class OpsServer:
    def __init__(self, url: str, received: list[Received]) -> None:
        self.url = url
        self.received = received


# meiva:note
# The gateway access the test client presents: the cluster secret as the bearer.
def rpc_access() -> GatewayAccess:
    return GatewayAccess(
        gateway_url="http://gateway",
        cluster_secret=RPC_TEST_CREDENTIAL,
        remote_plane=False,
        machine_token=None,
        launcher_profile=None,
        gateway_home=False,
    )


# meiva:note
# A client with the test credential and a manual clock for its retry waits, and that clock. The
# clock records each wait instead of sleeping, so a retry loop runs at full speed.
def rpc_client(
    *, timeout_seconds: float = 30.0, max_retries: int = 3
) -> tuple[RpcClient, ManualTimers]:
    timers = ManualTimers()
    client = RpcClient(
        config=RpcConfig(timeout_seconds=timeout_seconds, max_retries=max_retries),
        access=rpc_access,
        timers=timers,
    )
    return client, timers


# meiva:note
# The URL of a port nothing listens on: a connection to it is refused.
def unreachable_url() -> str:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = cast(int, probe.getsockname()[1])
    return f"http://127.0.0.1:{port}"


@contextmanager
def scripted_ops_server(*replies: Reply) -> Generator[OpsServer]:
    if not replies:
        raise ValueError("a scripted server needs at least one reply")
    received: list[Received] = []
    pending = list(replies)
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def do_POST(self) -> None:
            body = cast(
                dict[str, object], json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            )
            with lock:
                received.append(Received(self.path, self.headers.get("Authorization"), body))
                reply = pending.pop(0) if len(pending) > 1 else pending[0]
            if reply.drop:
                self.close_connection = True
                return
            self.send_response(reply.status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(reply.body)))
            self.end_headers()
            self.wfile.write(reply.body)

    with (
        ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server,
        ThreadPoolExecutor(max_workers=1, thread_name_prefix="rpc-test-http") as executor,
    ):
        serving = executor.submit(server.serve_forever, poll_interval=0.01)
        try:
            yield OpsServer(f"http://127.0.0.1:{server.server_port}", received)
        finally:
            server.shutdown()
            serving.result()

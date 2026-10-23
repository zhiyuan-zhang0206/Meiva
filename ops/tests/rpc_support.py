from __future__ import annotations

import json
from collections.abc import Callable, Generator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import cast

import httpx

from base.clock.tests.manual_timers import ManualTimers
from base.cluster.machine import GatewayAccess
from ops.cluster.rpc import RpcClient, RpcConfig

RPC_TEST_CREDENTIAL = "rpc-test"


def midpoint_jitter(low: float, high: float) -> float:
    assert (low, high) == (0.5, 1.5)
    return 1.0


class RecordingClients:
    def __init__(self, handler: Callable[[httpx.Request], httpx.Response]) -> None:
        self.handler = handler
        self.requests: list[httpx.Request] = []
        self.timeouts: list[httpx.Timeout] = []
        self.trust_env: list[bool] = []

    def __call__(self, *, timeout: httpx.Timeout, trust_env: bool) -> httpx.AsyncClient:
        self.timeouts.append(timeout)
        self.trust_env.append(trust_env)
        return httpx.AsyncClient(
            timeout=timeout, trust_env=trust_env, transport=httpx.MockTransport(self.handle)
        )

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.handler(request)


@dataclass(frozen=True)
class RpcHarness:
    rpc: RpcClient
    clients: RecordingClients
    timers: ManualTimers

    @property
    def request(self) -> httpx.Request:
        return self.clients.requests[-1]

    @property
    def requests(self) -> list[httpx.Request]:
        return self.clients.requests


def rpc_harness(handler: Callable[[httpx.Request], httpx.Response]) -> RpcHarness:
    clients = RecordingClients(handler)
    timers = ManualTimers()

    def access() -> GatewayAccess:
        return GatewayAccess(
            gateway_url="http://gateway", cluster_secret=RPC_TEST_CREDENTIAL, remote_plane=False
        )

    return RpcHarness(
        rpc=RpcClient(
            config=RpcConfig(timeout_seconds=30.0, max_retries=3),
            access=access,
            clients=clients,
            timers=timers,
            uniform=midpoint_jitter,
        ),
        clients=clients,
        timers=timers,
    )


@contextmanager
def local_ops_server() -> Generator[tuple[str, list[tuple[str, dict[str, object]]]]]:
    requests: list[tuple[str, dict[str, object]]] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def do_POST(self) -> None:
            body = cast(
                dict[str, object], json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            )
            requests.append((self.path, body))
            data = json.dumps({"status": "completed", "result": {"ok": 1}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    with (
        ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server,
        ThreadPoolExecutor(max_workers=1, thread_name_prefix="rpc-test-http") as executor,
    ):
        serving = executor.submit(server.serve_forever, poll_interval=0.01)
        try:
            yield f"http://127.0.0.1:{server.server_port}", requests
        finally:
            server.shutdown()
            serving.result()

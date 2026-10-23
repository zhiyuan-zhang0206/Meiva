from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import httpx

from base.clock.tests.manual_timers import ManualTimers
from base.cluster.machine import GatewayAccess
from ops.cluster.rpc import RpcClient, RpcConfig


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
    return RpcHarness(
        rpc=RpcClient(
            config=RpcConfig(timeout_seconds=30.0, max_retries=3),
            access=GatewayAccess(gateway_url="http://gateway", cluster_secret="rpc-test"),
            clients=clients,
            timers=timers,
            uniform=lambda _low, _high: 1.0,
        ),
        clients=clients,
        timers=timers,
    )

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import replace

import httpx

from base.clock.tests.manual_timers import ManualTimers
from base.cluster.machine import GatewayAccess
from ops.cluster.rpc import RpcClient, RpcConfig
from ops.tests.rpc_support import RecordingClients, midpoint_jitter


def defaults() -> None:
    attempts = 0

    def flaky(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts <= 2:
            raise httpx.ConnectError("blip")
        return httpx.Response(200, json={"status": "completed", "result": {}})

    clients = RecordingClients(flaky)
    timers = ManualTimers()
    rpc = replace(RpcClient.system(), clients=clients, timers=timers, uniform=midpoint_jitter)
    asyncio.run(rpc.dispatch_to_url("runner-a", "status_probe", {}, ops_url="http://host:8106"))
    sys.stdout.write(
        json.dumps(
            {
                "attempts": attempts,
                "timeouts": [timeout.read for timeout in clients.timeouts],
                "connect_timeouts": [timeout.connect for timeout in clients.timeouts],
                "sleeps": timers.sleeps,
                "authorization": [request.headers["Authorization"] for request in clients.requests],
            }
        )
    )


def credentials() -> None:
    events: list[str] = []
    attempts = 0

    def access() -> GatewayAccess:
        secret = "rpc-first" if not events else "rpc-second"
        events.append("credentials:1" if not events else "credentials:2")
        return GatewayAccess(
            gateway_url="http://gateway",
            cluster_secret=secret,
            remote_plane=False,
            machine_token=None,
            launcher_profile=None,
            gateway_home=False,
        )

    def post(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        events.append("post")
        if attempts == 1:
            raise httpx.ReadTimeout("response lost")
        return httpx.Response(200, json={"status": "completed", "result": {}})

    clients = RecordingClients(post)
    rpc = RpcClient(
        config=RpcConfig(timeout_seconds=3.0, max_retries=1),
        access=access,
        timers=ManualTimers(),
        clients=clients,
        uniform=midpoint_jitter,
    )
    asyncio.run(
        rpc.dispatch_to_url("runner-a", "spawn-launch", {"agent_id": 5}, ops_url="http://host:8106")
    )
    sys.stdout.write(
        json.dumps(
            {
                "events": events,
                "authorization": [request.headers["Authorization"] for request in clients.requests],
                "keys": [
                    json.loads(request.content)["idempotency_key"] for request in clients.requests
                ],
            }
        )
    )


if __name__ == "__main__":
    if sys.argv[1] == "defaults":
        defaults()
    elif sys.argv[1] == "credentials":
        credentials()
    else:
        raise SystemExit(f"unknown case {sys.argv[1]!r}")

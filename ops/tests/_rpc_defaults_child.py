from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import httpx

from base.clock.tests.manual_timers import ManualTimers
from ops.cluster.rpc import RpcClient
from ops.tests.rpc_support import RecordingClients


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
    rpc = replace(RpcClient.system(), clients=clients, timers=timers, uniform=lambda _a, _b: 1.0)
    asyncio.run(rpc.dispatch_to_url("runner-a", "status_probe", {}, ops_url="http://host:8106"))
    print(
        json.dumps(
            {
                "attempts": attempts,
                "timeouts": [timeout.read for timeout in clients.timeouts],
                "sleeps": timers.sleeps,
                "authorization": [request.headers["Authorization"] for request in clients.requests],
            }
        )
    )


if __name__ == "__main__":
    defaults()

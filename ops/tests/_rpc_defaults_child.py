# meiva:note
# The RPC client as a gateway-profile process builds it from its unit configuration.
#
# Runs in a child process whose environment is a real gateway profile and whose unit `.env`
# carries the timeout and retry settings, so `RpcClient.from_boot` reads real configuration
# without any settings attribute being replaced. Cases: `config` prints what the booted client
# was built with and presents; `proxy` dispatches to the loopback server named by `argv[2]` with
# a dead proxy in the environment.

from __future__ import annotations

import asyncio
import json
import sys

from base.clock import SYSTEM_TIMERS
from base.clock.tests.seeded_rng import seeded_rng
from base.cluster.machine import gateway_auth_headers
from base.config import ConfigBoot
from ops.cluster.rpc import RpcClient


def _booted_client() -> RpcClient:
    boot = ConfigBoot()
    boot.boot()
    return RpcClient.from_boot(boot, timers=SYSTEM_TIMERS, rng=seeded_rng())


def config(ops_url: str) -> None:
    client = _booted_client()
    asyncio.run(client.dispatch_to_url("runner-a", "status_probe", {}, ops_url=ops_url, retries=0))
    sys.stdout.write(
        json.dumps(
            {
                "timeout_seconds": client.config.timeout_seconds,
                "max_retries": client.config.max_retries,
                "authorization": gateway_auth_headers(client.access())["Authorization"],
            }
        )
    )


def proxy(ops_url: str) -> None:
    client = _booted_client()
    result = asyncio.run(
        client.dispatch_to_url("runner-a", "status_probe", {}, ops_url=ops_url, retries=0)
    )
    sys.stdout.write(json.dumps(result))


if __name__ == "__main__":
    case, url = sys.argv[1], sys.argv[2]
    if case == "config":
        config(url)
    elif case == "proxy":
        proxy(url)
    else:
        raise SystemExit(f"unknown case {case!r}")

# meiva:note
# The gateway's own machine as a runner whose ops daemon runs in this process.
#
# `services.agent_runner.agent_ops.daemon.ops_route` is the `/ops` handler of a runner's ops
# daemon: it parses the envelope, applies the idempotency record, runs the op against the data
# plane and wraps the outcome. This module serves that route on a real listener and registers the
# machine in `machines`, so a spawn, a lifecycle request or a config read that the gateway
# forwards to its own machine is executed by the real op code against the test's Postgres and
# Redis, over the same HTTP round trip a deployed runner answers.

from __future__ import annotations

import asyncio
import json
from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import psycopg

from base.cluster.machine import MachineIdentity
from base.config.service_read import ConfigAuthority
from base.db import Database
from base.host.system.hostport import Host
from base.lm.catalog import ModelCatalog
from base.native_process.loaded_commit import LoadedCommit
from gateway.tests.support.runner_ops.ops_stub import OpsCall, OpsReply, RunnerOps, runner_ops
from services.agent_runner.agent_ops.daemon import ops_route

__all__ = ["local_runner"]


# meiva:note
# Register `machine` as a gateway + agent-runner and answer its ops with the real route.
@contextmanager
def local_runner(
    conn: psycopg.Connection,
    *,
    database: Database,
    authority: ConfigAuthority,
    catalog: ModelCatalog,
    machine: MachineIdentity,
    host: Host,
    image: LoadedCommit,
) -> Generator[RunnerOps]:
    with database.pool(max_size=4) as pool, ThreadPoolExecutor(max_workers=4) as executor:

        def serve(call: OpsCall) -> OpsReply:
            envelope = {
                "kind": call.kind,
                "payload": call.payload,
                "idempotency_key": call.idempotency_key,
            }

            async def route() -> tuple[int, bytes, str]:
                return await ops_route(
                    json.dumps(envelope).encode(),
                    active_ops={},
                    workers=set(),
                    dispatch_sem=asyncio.Semaphore(4),
                    requests=set(),
                    pool=pool,
                    executor=executor,
                    catalog=catalog,
                    authority=authority,
                    database=lambda: database,
                    image=image,
                    local_machine=machine,
                    host=host,
                )

            status, body, _content_type = asyncio.run(route())
            return OpsReply(status, json.loads(body))

        with runner_ops(
            conn, machine.name(), roles=("gateway", "agent-runner"), otherwise=serve
        ) as runner:
            yield runner

# meiva:note
# The ops daemon's request path against the test's own data plane: real operations, real wedges.
#
# `Ops` holds the invocation-owned resources every dispatch needs (a pool on the throwaway
# Postgres, the op executor, the config authority of the test home) and runs the daemon's entry
# points over them, so a test names the operation and reads the answer. Operations are the real
# ones: `config_write` leaves one audit record per execution (its trace id marks which request
# executed it), which is how a test counts executions, and it blocks on the `.env` lock, which is
# how a test holds an operation in flight without a stand-in.

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Generator, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from psycopg_pool import ConnectionPool
from redis.client import PubSub

from base.clock.tests.seeded_rng import seeded_rng
from base.cluster import inbound_channel
from base.cluster.machine import MachineIdentityReader
from base.config import get_field
from base.config.service_read import ConfigAuthority
from base.daemon.endpoints import ServiceEndpoint, ServiceEndpoints
from base.db import Database
from base.events.live.bus import EventBus
from base.host.env import dotenv_file
from base.host.system.hostport import Host
from base.lm.catalog import ModelCatalog
from base.native_process.loaded_commit import LoadedCommit
from base.native_process.os_platform import file_lock
from base.paths import meiva_home, run_dir
from ops.env_audit import read_env_write_records
from ops.rpc_schemas import OpStatus
from ops.upload_delivery import ReceiverConfig
from services.agent_runner.agent_ops import daemon, in_flight

__all__ = ["Ops", "Wakes", "config_write", "executions", "held_env_lock", "watching_wakes"]

_AUDIT_WINDOW = 200


# meiva:note
# The payload of a `config_write` that sets the op concurrency, stamped with `trace_id`.
def config_write(trace_id: str, *, value: int = 3) -> dict[str, Any]:
    return {"overrides": {"ops_concurrency": value}, "trace_id": trace_id}


# meiva:note
# How many times a `config_write` stamped `trace_id` executed: one audit record each.
def executions(trace_id: str, *, env_path: Path) -> int:
    records = read_env_write_records(_AUDIT_WINDOW, env_path)
    return sum(1 for record in records if record.get("trace_id") == trace_id)


# meiva:note
# Hold the `.env` lock, so every `config_write` blocks inside its write until the block ends.
@contextmanager
def held_env_lock() -> Generator[None]:
    with file_lock(dotenv_file.env_lock_path(dotenv_file.process_env_file()), timeout_s=60.0):
        yield


@dataclass
class Ops:
    pool: ConnectionPool
    executor: ThreadPoolExecutor
    catalog: ModelCatalog
    authority: ConfigAuthority
    database: Callable[[], Database]
    image: LoadedCommit
    machine: MachineIdentityReader
    host: Host
    home: Path
    receiver: ReceiverConfig
    # meiva:note
    # The agent-host row this invocation's dispatch answers for: the test's own settings name the
    # port and the home names the runtime directory, exactly as the daemon's entry builds it.
    agent_host: ServiceEndpoint = field(
        default_factory=lambda: ServiceEndpoints.from_fields(
            get_field, runtime=run_dir(meiva_home())
        ).of("agent_host")
    )
    active_ops: daemon.ActiveOps = field(default_factory=dict)
    workers: in_flight.WorkerFutures = field(default_factory=set)
    requests: in_flight.RequestTokens = field(default_factory=set)

    async def dispatch(
        self, kind: str, payload: dict[str, Any]
    ) -> tuple[OpStatus, dict[str, object]]:
        return await daemon.dispatch(
            kind,
            payload,
            active_ops=self.active_ops,
            workers=self.workers,
            pool=self.pool,
            executor=self.executor,
            catalog=self.catalog,
            authority=self.authority,
            database=self.database,
            image=self.image,
            local_machine=self.machine,
            host=self.host,
            rng=seeded_rng(),
            home=self.home,
            receiver=self.receiver,
            agent_host=self.agent_host,
        )

    async def run_arm(
        self, kind: str, payload: dict[str, Any]
    ) -> tuple[OpStatus, dict[str, object]]:
        return await daemon.run_arm(
            kind,
            payload,
            active_ops=self.active_ops,
            workers=self.workers,
            pool=self.pool,
            executor=self.executor,
            authority=self.authority,
            database=self.database,
            image=self.image,
            local_machine=self.machine,
            host=self.host,
            rng=seeded_rng(),
            home=self.home,
            receiver=self.receiver,
            agent_host=self.agent_host,
        )

    async def idempotent(
        self,
        kind: str,
        payload: dict[str, Any],
        key: str,
        *,
        dedup_wait_attempts: int = 1,
        dedup_wait_step_s: float = 0.0,
        retry_backoff_s: float = 0.0,
        pass_only: bool = False,
    ) -> tuple[OpStatus, dict[str, object]]:
        entry = daemon.dispatch_idempotent_pass if pass_only else daemon.dispatch_idempotent
        tunables: dict[str, Any] = {
            "dedup_wait_attempts": dedup_wait_attempts,
            "dedup_wait_step_s": dedup_wait_step_s,
        }
        if not pass_only:
            tunables["retry_backoff_s"] = retry_backoff_s
        return await entry(
            kind,
            payload,
            key,
            self.pool,
            active_ops=self.active_ops,
            workers=self.workers,
            executor=self.executor,
            catalog=self.catalog,
            authority=self.authority,
            database=self.database,
            image=self.image,
            local_machine=self.machine,
            host=self.host,
            rng=seeded_rng(),
            home=self.home,
            receiver=self.receiver,
            agent_host=self.agent_host,
            **tunables,
        )

    async def route(
        self, envelope: Mapping[str, object] | bytes, *, concurrency: int = 4
    ) -> tuple[int, dict[str, Any]]:
        body = envelope if isinstance(envelope, bytes) else json.dumps(envelope).encode()
        status, answer, _content_type = await self.route_raw(body, asyncio.Semaphore(concurrency))
        return status, json.loads(answer)

    async def route_raw(
        self, body: bytes, dispatch_sem: asyncio.Semaphore
    ) -> tuple[int, bytes, str]:
        return await daemon.ops_route(
            body,
            active_ops=self.active_ops,
            workers=self.workers,
            dispatch_sem=dispatch_sem,
            requests=self.requests,
            pool=self.pool,
            executor=self.executor,
            catalog=self.catalog,
            authority=self.authority,
            database=self.database,
            image=self.image,
            local_machine=self.machine,
            host=self.host,
            rng=seeded_rng(),
            home=self.home,
            receiver=self.receiver,
            agent_host=self.agent_host,
        )


# meiva:note
# The wake messages published on an agent's inbound channel, counted from a real subscription.
class Wakes:
    def __init__(self, pubsub: PubSub) -> None:
        self._pubsub = pubsub

    def received(self, *, wait_s: float = 1.0) -> int:
        count = 0
        while self._pubsub.get_message(timeout=wait_s if count == 0 else 0.1) is not None:
            count += 1
        return count


# meiva:note
# Subscribe to an agent's inbound channel; an operation that wakes the agent is counted by
# `Wakes.received`.
@contextmanager
def watching_wakes(event_bus: EventBus, agent_id: int) -> Generator[Wakes]:
    with (
        event_bus.sync_redis(decode_responses=True) as redis,
        PubSub(redis.connection_pool) as pubsub,
    ):
        pubsub.subscribe(inbound_channel(agent_id, event_bus.channel))
        subscribed = cast("dict[str, object] | None", pubsub.get_message(timeout=2))
        assert subscribed is not None and subscribed["type"] == "subscribe"
        yield Wakes(pubsub)

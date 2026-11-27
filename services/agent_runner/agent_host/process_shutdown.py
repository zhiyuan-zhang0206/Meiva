# meiva:link services/agent_runner/agent_host/lifecycle/docs/graceful-exit.md
import asyncio
import logging
from contextlib import AsyncExitStack
from pathlib import Path

import psycopg
from psycopg_pool import AsyncConnectionPool

from base.agents.context.clients import ClientSet
from base.daemon.health import stop_health_server
from base.lm.usage.receipt import UsageReceiptRuntime
from base.native_process.stop_timing import CANCEL_UNWIND_TIMEOUT_S
from meiva.sdk_surface.installation import Installation

from ...pidfile import remove_pidfile
from .dispatcher import TurnScheduler
from .host import AgentHost
from .host_beat import close_ownership_beat
from .lifecycle.instance import release_instance_lock
from .pools import close_host_pools

host_log = logging.getLogger("services.agent_runner.agent_host.daemon")


async def close_host_runtime(
    host: AgentHost,
    scheduler: TurnScheduler,
    beat: asyncio.Task[Exception | None] | None,
    beat_tasks: asyncio.TaskGroup,
) -> None:
    resource_deadline = asyncio.get_running_loop().time() + CANCEL_UNWIND_TIMEOUT_S
    async with AsyncExitStack() as cleanup:
        cleanup.push_async_callback(host.aclose, resource_deadline=resource_deadline)
        cleanup.push_async_callback(close_ownership_beat, beat, beat_tasks)
        cleanup.push_async_callback(scheduler.aclose)


async def close_joined_host_pools(
    host: AgentHost | None,
    workload_pool: AsyncConnectionPool[psycopg.AsyncConnection],
    control_pool: AsyncConnectionPool[psycopg.AsyncConnection],
) -> None:
    if host is None or host.resources_joined:
        await close_host_pools(workload_pool, control_pool)
    else:
        host_log.error("[agent-host] resource join unfinished; keeping pools open until hard exit")


async def close_process_owners(
    host: AgentHost | None,
    workload_pool: AsyncConnectionPool[psycopg.AsyncConnection],
    control_pool: AsyncConnectionPool[psycopg.AsyncConnection],
    installation: Installation | None,
    health: asyncio.Server | None,
    beat: asyncio.Task[Exception | None] | None,
    beat_tasks: asyncio.TaskGroup | None,
    *,
    clients: ClientSet | None = None,
    usage_receipts: UsageReceiptRuntime | None = None,
    instance_lock: int | None = None,
    pidfile: Path,
    primary: BaseException | None = None,
) -> None:
    try:
        async with AsyncExitStack() as cleanup:
            if instance_lock is not None:
                cleanup.callback(release_instance_lock, instance_lock)
            cleanup.callback(remove_pidfile, pidfile)
            cleanup.push_async_callback(close_joined_host_pools, host, workload_pool, control_pool)
            if host is None and clients is not None:
                cleanup.callback(clients.close)
            if usage_receipts is not None:
                _ = cleanup.push_async_callback(
                    usage_receipts.aclose, staging_timeout=8.0, delivery_timeout=1.0
                )
            if installation is not None:
                cleanup.callback(installation.sampling.close)
            if health is not None:
                cleanup.push_async_callback(stop_health_server, health)
            cleanup.push_async_callback(close_ownership_beat, beat, beat_tasks)
    except BaseException as secondary:
        if primary is None:
            raise
        primary.add_note(f"Host process cleanup also failed: {secondary!r}")

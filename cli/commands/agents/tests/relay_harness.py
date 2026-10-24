# meiva:note
# What the relay's real-resource tests share: the lease a relay serves, a relay running as a task
# beside the test body, a bounded wait for a condition, the tuning that makes it poll quickly,
# and the stdout a Claude Monitor reads.

from __future__ import annotations

import asyncio
import io
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from typing import Any
from uuid import UUID

from base.cluster.machine import MachineIdentity
from base.db import Database
from base.events.live.bus import EventBus
from base.events.live.redis_listener import RedisInboundListener
from cli.commands.agents.impersonation_adapters import RelayAdapter
from cli.commands.agents.impersonation_relay import RelayLease, push_inbox
from cli.commands.agents.relay.steps import RelayTuning

__all__ = ["CSRF", "FAST", "SLOW", "THREAD", "Printed", "relay_lease", "running", "until"]

THREAD = UUID("b9d32d0d-bd27-40fc-83e8-692769b21523")
FAST = RelayTuning(
    debounce=0.0,
    max_chars=2000,
    catchup_seconds=0.2,
    min_emit_interval=0.0,
    min_wake_seconds=0.05,
)
SLOW = 30.0
CSRF = "recorded-csrf"


def relay_lease(lease: dict[str, Any], machine: MachineIdentity) -> RelayLease:
    return RelayLease(
        agent_id=lease["agent_id"],
        lease_id=UUID(lease["id"]),
        session_id=lease["session_id"],
        token=lease["relay_token"],
        machine=machine.name(),
    )


@asynccontextmanager
async def running(
    lease: RelayLease,
    *,
    database: Database,
    event_bus: EventBus,
    redis_url: str,
    adapter: RelayAdapter,
    tuning: RelayTuning = FAST,
) -> AsyncIterator[asyncio.Task[None]]:
    listener = RedisInboundListener(redis_url, lease.agent_id)
    task = asyncio.create_task(
        push_inbox(
            lease, db=database, bus=event_bus, listener=listener, adapter=adapter, tuning=tuning
        )
    )
    try:
        yield task
    finally:
        if not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


async def until(condition: Callable[[], bool], what: str) -> None:
    deadline = asyncio.get_running_loop().time() + SLOW
    while not condition():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        await asyncio.sleep(0.02)


# meiva:note
# The stdout a Claude Monitor reads the relay's pushes from: the stream the adapter is bound to.
class Printed:
    def __init__(self) -> None:
        self.stream = io.StringIO()

    @property
    def text(self) -> str:
        return self.stream.getvalue()

    def contains(self, needle: str) -> bool:
        return needle in self.text

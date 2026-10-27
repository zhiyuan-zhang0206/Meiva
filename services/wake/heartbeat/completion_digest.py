"""Hourly completion-notice digest delivery, a loop of the heartbeat service.

Background-command and watcher completions are recorded as `completion_notice_events`
rows; once an hour has completed, the loop delivers one digest per agent and hour as
a chat inbound (source `system:completion-digest`) and marks the rows. A system
notice never resurrects a terminated owner (`ops.lifecycle.resurrect_if_terminated`),
so the digest waits in the owner's queue like any other framework notification.

The delivery key `completion-digest:<agent>:<hour>` makes a crash between the delivery
and the mark exactly once: the retry meets the existing receipt. Unknown delivery
errors propagate through this caller to the heartbeat service's
`TaskGroup`; a later service restart reconciles the same durable key.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from psycopg_pool import ConnectionPool

from base.clock import Clock
from base.cluster.machine import MachineIdentityReader
from base.daemon import round_loop
from base.daemon.loop_health import LoopProgress
from base.daemon.schedules.completion_notices import (
    CompletionDigest,
    format_digest,
    mark_digest_delivered,
    pending_digests,
    prune_delivered_notices,
)
from base.db import Database
from base.events.live.bus import EventBus
from ops.cluster.rpc import RpcClient
from ops.lifecycle.delivery import deliver_chat_inbound

FLUSH_INTERVAL_S = 60.0
_SOURCE = "system:completion-digest"
_EVENT_RETENTION = timedelta(days=7)


def _pending(pool: ConnectionPool, now: datetime) -> list[CompletionDigest]:
    with pool.connection() as conn:
        return pending_digests(conn, now)


def _mark_delivered(pool: ConnectionPool, digest: CompletionDigest, inbound_id: int) -> None:
    with pool.connection() as conn:
        mark_digest_delivered(conn, digest.event_ids, inbound_id)
        conn.commit()


def _prune_delivered(pool: ConnectionPool, before: datetime) -> int:
    with pool.connection() as conn:
        pruned = prune_delivered_notices(conn, before)
        conn.commit()
    return pruned


def _digest_key(digest: CompletionDigest) -> str:
    """A stable receipt key makes send-before-mark recovery exactly once."""
    return f"completion-digest:{digest.agent_id}:{digest.window_start.astimezone(UTC).isoformat()}"


async def flush_once(
    pool: ConnectionPool,
    db: Database,
    bus: EventBus,
    *,
    now: datetime,
    machine: MachineIdentityReader,
    rpc: RpcClient,
) -> int:
    """Deliver each completed hour, then mark its persisted event rows.

    A crash after delivery but before the mark repeats the same idempotency key,
    obtaining the existing inbound receipt instead of a second digest.
    """
    moment = now
    digests = await asyncio.to_thread(_pending, pool, moment)
    delivered = 0
    for digest in digests:
        delivery = await deliver_chat_inbound(
            pool,
            db,
            bus,
            digest.agent_id,
            prepare=lambda _conn, digest=digest: format_digest(
                agent_id=digest.agent_id,
                window_start=digest.window_start,
                notices=digest.notices,
            ),
            source=_SOURCE,
            client_message_id=_digest_key(digest),
            local_machine=machine.name(),
            rpc=rpc,
        )
        if delivery.inbound_id is None:
            raise RuntimeError("completion-notice digest delivery returned no inbound receipt")
        await asyncio.to_thread(_mark_delivered, pool, digest, delivery.inbound_id)
        delivered += 1
    await asyncio.to_thread(_prune_delivered, pool, moment - _EVENT_RETENTION)
    return delivered


@dataclass(frozen=True)
class DigestConfig:
    flush_interval_s: float = FLUSH_INTERVAL_S


class DigestRound(Protocol):
    @property
    def interval_s(self) -> float: ...

    async def run_round(self) -> None: ...


# meiva:link services/wake/heartbeat/docs/completion-digest.meiva.okf.md
@dataclass(frozen=True)
class CompletionDigestRound:
    pool: ConnectionPool
    db: Database
    bus: EventBus
    machine: MachineIdentityReader
    clock: Clock
    config: DigestConfig
    rpc: RpcClient

    @property
    def interval_s(self) -> float:
        return self.config.flush_interval_s

    async def run_round(self) -> None:
        await flush_once(
            self.pool,
            self.db,
            self.bus,
            machine=self.machine,
            now=self.clock.now(),
            rpc=self.rpc,
        )


# meiva:link services/wake/heartbeat/docs/completion-digest.meiva.okf.md
async def completion_digest_loop(digest: DigestRound, progress: LoopProgress) -> None:
    await round_loop.run_rounds("completion-digest", progress, digest.interval_s, digest.run_round)

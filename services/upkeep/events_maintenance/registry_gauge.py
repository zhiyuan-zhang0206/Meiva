# meiva:note
# Agent-registry max-id gauge loop of `events_maintenance`: samples `max(agents.id)` once per
# `FLUSH_INTERVAL_S` and emits an `agent_registry` telemetry event exported to Prometheus as the
# unit-"1" ObservableGauge `meiva_agent_registry_max_id_ratio` (same suffix as
# `checkpoint_table_sizes`). Unreachable DB skips the sample; other errors end the loop for
# supervisor restart.

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from psycopg_pool import ConnectionPool

from base import telemetry
from base.daemon import round_loop
from base.daemon.loop_health import LoopProgress
from base.db.rows import query_one

# One sample per minute — bounded row rate, same cadence as the auth-401 and
# latency flushers. A 60s gauge is plenty for a growth curve that changes
# only when agents are spawned.
FLUSH_INTERVAL_S = 60.0


@dataclass(frozen=True)
class _MaxRow:
    max_id: int | None


def read_max_agent_id_blocking(pool: ConnectionPool) -> int | None:
    """The registry high-water mark: ``SELECT max(id) FROM agents``.

    Runs on a connection borrowed from the pool (blocking — call via
    ``asyncio.to_thread`` from the loop). Returns ``None`` when the table
    is empty; an empty registry is not a number worth emitting.
    """
    with pool.connection() as conn:
        row = query_one(conn, _MaxRow, "SELECT max(id) AS max_id FROM agents")
    return None if row is None else row.max_id


def emit_max_agent_id(max_id: int) -> None:
    """Emit one ``agent_registry`` telemetry event carrying ``max_id``.

    Exposed separately from the loop so tests can drive it directly
    (mirrors ``latency.emit_bucket`` / ``rejection_log.emit_auth401_count``).
    """
    telemetry.emit("telemetry", "agent_registry", attributes={"max_id": max_id})


async def registry_gauge_round(pool: ConnectionPool) -> None:
    """Sample the registry max id once and emit it (nothing for an empty registry)."""
    max_id = await asyncio.to_thread(read_max_agent_id_blocking, pool)
    if max_id is not None:
        emit_max_agent_id(max_id)


async def registry_gauge_loop(pool: ConnectionPool, progress: LoopProgress) -> None:
    """Sample the registry max id every `FLUSH_INTERVAL_S` and emit, as a resident
    sequential loop (one sample at once, so a restart leaves no gap)."""

    async def one_round() -> None:
        await registry_gauge_round(pool)

    await round_loop.run_rounds("registry-gauge", progress, FLUSH_INTERVAL_S, one_round)

"""How the API reaches a schedule's session.

The sessions are kept alive by the `schedule-manager` service, a separate
process, so the API neither calls it nor holds its state. `request_sync` queues
a row in `schedule_sync_requests` (the service consumes it within about a
second, `services/wake/schedule_manager/requests.py`) and waits a bounded time for the
row to be consumed, so a start / stop / restart answers with the converged
status when the service is up and still answers when it is not. `capture` reads
the session's recent output straight from the shell backend: it needs no
manager state.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from psycopg import Connection
from psycopg_pool import ConnectionPool

from base.clock import Timers
from base.cluster import session_name
from base.db.transaction import write_transaction
from base.sessions.protocol import SessionBackend

_log = logging.getLogger(__name__)


def enqueue_blocking(pool: ConnectionPool[Any], schedule_id: int) -> None:
    """Queue (or refresh) the schedule's sync request. The row has no foreign
    key: a delete asks for the orphaned session to be killed after the schedule
    row is already gone."""
    with write_transaction(pool) as conn:
        enqueue_in_transaction(conn, schedule_id)


def enqueue_in_transaction(conn: Connection[Any], schedule_id: int) -> None:
    """Persist convergence work in the transaction that changes desired state.

    The caller owns commit/rollback; no schedule foreign key allows deletion
    to leave a durable request to reap its orphaned session.
    """
    conn.execute(
        "INSERT INTO schedule_sync_requests (schedule_id) VALUES (%s) "
        "ON CONFLICT (schedule_id) DO UPDATE SET requested_at = clock_timestamp()",
        (schedule_id,),
    )


def _is_queued_blocking(pool: ConnectionPool[Any], schedule_id: int) -> bool:
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT 1 FROM schedule_sync_requests WHERE schedule_id = %s", (schedule_id,))
        return cur.fetchone() is not None


# meiva:note
# Wait up to `wait_s` (the service's one-second poll plus the kill and relaunch it runs)
# for the request to be consumed, looking every `poll_s`.
async def wait_consumed(
    pool: ConnectionPool[Any], schedule_id: int, *, wait_s: float, poll_s: float, timers: Timers
) -> None:
    deadline = timers.monotonic() + wait_s
    while timers.monotonic() < deadline:
        if not await asyncio.to_thread(_is_queued_blocking, pool, schedule_id):
            return
        await timers.asleep(poll_s)
    _log.warning(
        "schedule %s: sync request not consumed within %.0fs — the schedule-manager "
        "service will converge it when it is next up",
        schedule_id,
        wait_s,
    )


async def request_sync(
    pool: ConnectionPool[Any], schedule_id: int, *, wait_s: float, poll_s: float, timers: Timers
) -> None:
    """Ask the schedule-manager service to converge one schedule's session to its
    latest desired revision now (adopt an applied revision, or replace
    its predecessor), and wait a bounded time for it to do so."""
    await asyncio.to_thread(enqueue_blocking, pool, schedule_id)
    await wait_consumed(pool, schedule_id, wait_s=wait_s, poll_s=poll_s, timers=timers)


async def capture(backend: SessionBackend, schedule_id: int, lines: int) -> str | None:
    """The schedule session's recent output, or None when no session is live."""
    return await asyncio.to_thread(capture_blocking, backend, schedule_id, lines)


def capture_blocking(backend: SessionBackend, schedule_id: int, lines: int) -> str | None:
    name = session_name(f"schedule-{schedule_id}")
    if not backend.has_session(name):
        return None
    return backend.capture_pane(name, lines)

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
from collections.abc import Callable
from pathlib import Path
from typing import Any

from psycopg import Connection
from psycopg_pool import ConnectionPool

from base.clock import Timers
from base.cluster import session_name
from base.db.transaction import write_transaction
from base.sessions.protocol import SessionBackend

_log = logging.getLogger(__name__)


def enqueue_blocking(pool: ConnectionPool[Any], schedule_id: int, machine: str = "gateway") -> None:
    with write_transaction(pool) as conn:
        enqueue_in_transaction(conn, schedule_id, machine)


def enqueue_in_transaction(conn: Connection[Any], schedule_id: int, machine: str) -> None:
    conn.execute(
        "INSERT INTO schedule_sync_requests (schedule_id, machine) VALUES (%s, %s) "
        "ON CONFLICT (schedule_id, machine) DO UPDATE SET requested_at = clock_timestamp()",
        (schedule_id, machine),
    )


def _is_queued_blocking(
    pool: ConnectionPool[Any], schedule_id: int, machine: str | None = None
) -> bool:
    with pool.connection() as conn, conn.cursor() as cur:
        if machine is not None:
            cur.execute(
                "SELECT 1 FROM schedule_sync_requests WHERE schedule_id = %s AND machine = %s",
                (schedule_id, machine),
            )
        else:
            cur.execute(
                "SELECT 1 FROM schedule_sync_requests WHERE schedule_id = %s", (schedule_id,)
            )
        return cur.fetchone() is not None


# meiva:note
# Wait up to `wait_s` (the service's one-second poll plus the kill and relaunch it runs)
# for the request to be consumed, looking every `poll_s`.
async def wait_consumed(
    pool: ConnectionPool[Any],
    schedule_id: int,
    *,
    machine: str | None = None,
    wait_s: float,
    poll_s: float,
    timers: Timers,
) -> None:
    deadline = timers.monotonic() + wait_s
    while timers.monotonic() < deadline:
        if not await asyncio.to_thread(_is_queued_blocking, pool, schedule_id, machine):
            return
        await timers.asleep(poll_s)
    _log.warning(
        "schedule %s: sync request not consumed within %.0fs — the schedule-manager "
        "service will converge it when it is next up",
        schedule_id,
        wait_s,
    )


async def request_sync(
    pool: ConnectionPool[Any],
    schedule_id: int,
    machine: str = "gateway",
    *,
    wait_s: float,
    poll_s: float,
    timers: Timers,
) -> None:
    """Ask the schedule-manager service to converge one schedule's session to its
    latest desired revision now (adopt an applied revision, or replace
    its predecessor), and wait a bounded time for it to do so."""
    await asyncio.to_thread(enqueue_blocking, pool, schedule_id, machine)
    await wait_consumed(
        pool, schedule_id, machine=machine, wait_s=wait_s, poll_s=poll_s, timers=timers
    )


async def capture(backend: SessionBackend, schedule_id: int, lines: int) -> str | None:
    """The schedule session's recent output, or None when no session is live."""
    return await asyncio.to_thread(capture_blocking, backend, schedule_id, lines)


def capture_blocking(backend: SessionBackend, schedule_id: int, lines: int) -> str | None:
    name = session_name(f"schedule-{schedule_id}")
    if not backend.has_session(name):
        return None
    return backend.capture_pane(name, lines)


def plugin_schedules_stat_digest(home: Path) -> tuple[tuple[str, int, int], ...]:
    entries: list[tuple[str, int, int]] = []
    pcfg = home / "plugins_config.json"
    try:
        st = pcfg.stat()
        entries.append(("plugins_config.json", st.st_mtime_ns, st.st_size))
    except OSError:
        entries.append(("plugins_config.json", 0, -1))
    configs_dir = home / "configs"
    if configs_dir.is_dir():
        try:
            children = sorted(configs_dir.iterdir())
        except OSError:
            return tuple(entries)
        for child in children:
            try:
                st = (child / "config.json").stat()
                entries.append((f"{child.name}/config.json", st.st_mtime_ns, st.st_size))
            except OSError:
                continue
    return tuple(entries)


def _run_with_conn(pool: ConnectionPool[Any], action: Callable[[Any], object]) -> None:
    with pool.connection() as conn:
        action(conn)


async def sync_startup_schedules(
    db_pool: ConnectionPool[Any],
    *,
    home: Path,
    machine: str,
    provision_builtins: bool,
) -> None:
    if provision_builtins:
        from base.daemon.schedules.builtin_schedules import provision_builtin_schedules

        try:
            await asyncio.to_thread(
                _run_with_conn,
                db_pool,
                lambda conn: provision_builtin_schedules(conn, machine=machine),
            )
        except Exception:
            _log.warning("gateway built-in schedule provisioning failed", exc_info=True)

    from base.daemon.schedules.plugin_schedules import reconcile_plugin_schedules

    try:
        await asyncio.to_thread(
            _run_with_conn, db_pool, lambda conn: reconcile_plugin_schedules(conn, home=home)
        )
    except Exception:
        _log.warning("gateway plugin schedule reconciliation failed", exc_info=True)


async def run_plugin_schedules_reconciler(
    pool: ConnectionPool[Any], home: Path, *, interval_seconds: float = 5.0
) -> None:
    from base.daemon.schedules.plugin_schedules import reconcile_plugin_schedules

    last_digest = plugin_schedules_stat_digest(home)
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            current_digest = plugin_schedules_stat_digest(home)
            if current_digest != last_digest:
                last_digest = current_digest
                await asyncio.to_thread(
                    _run_with_conn, pool, lambda conn: reconcile_plugin_schedules(conn, home=home)
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.warning("gateway plugin schedule background reconciler failed", exc_info=True)

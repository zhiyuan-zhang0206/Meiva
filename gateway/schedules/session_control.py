# meiva:note
# Provision built-in and plugin schedules.

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from psycopg import Connection
from psycopg.rows import class_row
from psycopg_pool import ConnectionPool

from base.clock import Clock

_log = logging.getLogger(__name__)


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


def _run_with_conn[T](pool: ConnectionPool, action: Callable[[Connection], T]) -> T:
    with pool.connection() as conn:
        return action(conn)


async def sync_startup_schedules(
    db_pool: ConnectionPool,
    *,
    home: Path,
    clock: Clock,
    resolve_machine: Callable[[], str],
    provision_builtins: bool,
) -> None:
    if provision_builtins:
        from base.daemon.schedules.builtin_schedules import provision_builtin_schedules

        try:
            machine = resolve_machine()
            await asyncio.to_thread(
                _run_with_conn,
                db_pool,
                lambda conn: provision_builtin_schedules(
                    conn,
                    machine=machine,
                    cluster_timezone=clock.timezone,
                    now=clock.now(),
                ),
            )
        except Exception:
            _log.warning("gateway built-in schedule provisioning failed", exc_info=True)

    from base.daemon.schedules.plugin_schedules import reconcile_plugin_schedules

    try:
        await asyncio.to_thread(
            _run_with_conn,
            db_pool,
            lambda conn: reconcile_plugin_schedules(
                conn,
                home=home,
                cluster_timezone=clock.timezone,
                now=clock.now(),
            ),
        )
    except Exception:
        _log.warning("gateway plugin schedule reconciliation failed", exc_info=True)


@dataclass(frozen=True)
class _RunnerMachineRow:
    name: str


def _query_runner_machines(conn: Connection) -> tuple[str, ...]:
    with conn.cursor(row_factory=class_row(_RunnerMachineRow)) as cur:
        cur.execute("SELECT name FROM machines WHERE 'agent-runner' = ANY(role) ORDER BY name")
        rows = cur.fetchall()
    return tuple(r.name for r in rows)


async def run_plugin_schedules_reconciler(
    pool: ConnectionPool,
    home: Path,
    *,
    clock: Clock,
    interval_seconds: float = 5.0,
    resolve_machine: Callable[[], str] | None = None,
    provision_builtins: bool = False,
) -> None:
    from base.daemon.schedules.builtin_schedules import provision_builtin_schedules
    from base.daemon.schedules.plugin_schedules import reconcile_plugin_schedules

    last_digest = plugin_schedules_stat_digest(home)
    last_runners: tuple[str, ...] | None = None
    if provision_builtins and resolve_machine is not None:
        try:
            last_runners = await asyncio.to_thread(_run_with_conn, pool, _query_runner_machines)
        except Exception:
            _log.warning("failed to query runner machines on startup", exc_info=True)

    while True:
        await asyncio.sleep(interval_seconds)
        try:
            current_digest = plugin_schedules_stat_digest(home)
            if current_digest != last_digest:
                last_digest = current_digest
                await asyncio.to_thread(
                    _run_with_conn,
                    pool,
                    lambda conn: reconcile_plugin_schedules(
                        conn,
                        home=home,
                        cluster_timezone=clock.timezone,
                        now=clock.now(),
                    ),
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.warning("gateway plugin schedule background reconciler failed", exc_info=True)

        if provision_builtins and resolve_machine is not None:
            try:
                current_runners = await asyncio.to_thread(
                    _run_with_conn, pool, _query_runner_machines
                )
                if current_runners != last_runners:
                    last_runners = current_runners
                    machine = resolve_machine()
                    await asyncio.to_thread(
                        _run_with_conn,
                        pool,
                        lambda conn, m=machine: provision_builtin_schedules(
                            conn,
                            machine=m,
                            cluster_timezone=clock.timezone,
                            now=clock.now(),
                        ),
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                _log.warning("gateway built-in schedule runner reconciler failed", exc_info=True)

# meiva:note
# The reaper's database-only sweep: expired browser sessions, plus the slow
# maintenance phases on durable cadences.
#
# One resident loop (`sweep_loop`) runs `sweep_round` every
# `MEIVA_TTL_REAPER_POLL_INTERVAL_SECONDS`. A round runs its phases in order; each
# phase is a bounded batch, and a backlog drains over successive rounds:
#
# - **Browser sessions** — expired ``web_sessions`` rows are deleted here, so
#   cleanup does not depend on the next login.
# - **Schedule fire log** — ``schedule_fire_log`` claims older than the configured
#   retention window (30 days by default) are deleted once per day.
#
# The slow phases are claimed in ``maintenance_state`` (``cadence``), so a restart
# resumes their cadence.
#
# Every reclaim is a CAS that loses cleanly to ``close()`` / termination. The sweep
# has no remote calls; shell kills and work-failure redelivery live in the
# ``remote`` loop so a slow machine never delays the database-only phases.

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from psycopg_pool import ConnectionPool

from base.daemon import round_loop
from base.daemon.loop_health import LoopProgress
from base.db.transaction import write_transaction
from services.upkeep.ttl_reaper import cadence
from services.upkeep.ttl_reaper.config import TtlReaperConfig
from services.upkeep.ttl_reaper.owner_notice import PASS_BATCH

_log = logging.getLogger(__name__)

# Bound for the per-pass schedule_fire_log retention prune. The tables are
# small by design; the cap keeps a backlog (e.g. after a long outage) from
# turning one pass into a multi-minute transaction.
FIRE_LOG_PASS_BATCH = 50_000


def reap_expired_web_sessions_blocking(
    pool: ConnectionPool, *, batch_size: int = PASS_BATCH
) -> int:
    """Delete browser sessions whose authoritative expiry has elapsed."""
    with write_transaction(pool) as conn, conn.cursor() as cur:
        cur.execute(
            "WITH expired AS ("
            "SELECT id FROM web_sessions WHERE expires_at < now() "
            "ORDER BY expires_at, id LIMIT %s"
            ") DELETE FROM web_sessions WHERE id IN (SELECT id FROM expired)",
            (batch_size,),
        )
        return cur.rowcount


# meiva:note
# Delete `schedule_fire_log` claims older than the retention window.
#
# The table is the at-most-once claim ledger of `fire_slot_once` (`schedules/jobs/catchup.py`);
# a claim only matters while its slot can still be fired, so every row past the window is
# deleted. One bounded DELETE per pass: a backlog (long gateway outage, many schedules) drains
# over successive passes instead of one long transaction.
def prune_schedule_fire_log_blocking(
    pool: ConnectionPool,
    *,
    retention_days: int,
    now: datetime,
    batch_size: int = FIRE_LOG_PASS_BATCH,
) -> int:
    cutoff = now - timedelta(days=retention_days)
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            DELETE FROM schedule_fire_log
            WHERE id IN (
                SELECT id
                FROM schedule_fire_log
                WHERE slot_fire_at < %s
                ORDER BY id
                LIMIT %s
            )
            """,
            (cutoff, batch_size),
        )
        return cur.rowcount


# meiva:note
# The cadence-gated phases that came due: the number of pruned fire-log rows.
async def _slow_phases(pool: ConnectionPool, config: TtlReaperConfig) -> int:
    if not await asyncio.to_thread(
        cadence.claim_due, pool, cadence.FIRE_LOG_PRUNE, config.fire_log_cleanup_interval_s
    ):
        return 0
    return await asyncio.to_thread(
        prune_schedule_fire_log_blocking,
        pool,
        retention_days=config.fire_log_retention_days,
        now=config.clock.now(),
    )


async def sweep_round(
    pool: ConnectionPool,
    progress: LoopProgress,
    config: TtlReaperConfig,
) -> None:
    """One pass over every database-only phase, beating `progress` between them."""
    sessions = await asyncio.to_thread(reap_expired_web_sessions_blocking, pool)
    progress.beat()
    pruned_fire_log = await _slow_phases(pool, config)
    if sessions or pruned_fire_log:
        _log.info(
            "[ttl-reaper] reclaimed %d web session(s); pruned %d schedule fire-log row(s)",
            sessions,
            pruned_fire_log,
        )


async def sweep_loop(
    pool: ConnectionPool,
    progress: LoopProgress,
    config: TtlReaperConfig,
) -> None:
    """The database-only sweep as a resident sequential loop."""

    async def one_round() -> None:
        await sweep_round(pool, progress, config)

    await round_loop.run_rounds("sweep", progress, config.poll_interval_s, one_round)

# meiva:note
# The reaper's database-only sweep: expired pages, browser sessions and notices,
# plus the slow maintenance phases on durable cadences.
#
# One resident loop (`sweep_loop`) runs `sweep_round` every
# `MEIVA_TTL_REAPER_POLL_INTERVAL_SECONDS`. A round runs its phases in order; each
# phase is a bounded batch, and a backlog drains over successive rounds:
#
# - **Pages** — the row is terminalized with ``expired_at`` (the reverse proxy
#   then answers the page's link with the friendly "page expired" notice), a
#   ``PageClosed`` event is published so the frontend drops the entry, and the
#   page-server daemon stops the page session when its reconcile sees the row
#   leave the open set (the daemon's existing two-layer teardown). Pages have a
#   hard lifetime (user ruling 2026-08-25; default 24h, ``MEIVA_PAGE_DEFAULT_TTL_SECONDS``).
# - **Browser sessions** — expired ``web_sessions`` rows are deleted here, so
#   cleanup does not depend on the next login.
# - **Notices** — ``agent_notices`` past ``expire_at`` auto-resolve.
# - **Schedule fire log** — ``schedule_fire_log`` claims older than the configured
#   retention window (30 days by default) are deleted once per day.
#
# The slow phases are claimed in ``maintenance_state`` (``cadence``), so a restart
# resumes their cadence. Owners are notified (inbound, source ``"system"``) only
# when the agent is running or idling — a terminated agent's page expiring is
# exactly the cleanup the TTL exists for, and must not resurrect it. The one
# exception is notice expiry: that message reports the owner's own notice, so it
# is queued for a terminated owner too while still publishing no wake — read if
# the agent comes back within the stale-chat window (24h by default), retired as
# history after it. Such a row is not a delivery stall (the watchdog skips system
# rows of a dead owner). Expiry is silent for the user (ruling 2026-10-21): the
# notice only leaves the open queue, and no new notification appears.
#
# Every reclaim is a CAS that loses cleanly to ``close()`` / termination. The sweep
# has no remote calls; shell kills and work-failure redelivery live in the
# ``remote`` loop so a slow machine never delays a page expiry.

from __future__ import annotations

import asyncio
import functools
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from psycopg_pool import ConnectionPool

from base.daemon import round_loop
from base.daemon.loop_health import LoopProgress
from base.db import Database
from base.db.rows import query_all
from base.db.transaction import write_transaction
from base.events.live.announce import publish_agent_updated_sync
from base.events.live.bus import EventBus
from ops import lifecycle
from services.upkeep.ttl_reaper import cadence, owner_text
from services.upkeep.ttl_reaper.config import TtlReaperConfig
from services.upkeep.ttl_reaper.owner_notice import (
    PASS_BATCH,
    OwnerMessage,
    notify_owner,
    wake_owners,
)

_log = logging.getLogger(__name__)

# Bound for the per-pass schedule_fire_log retention prune. The tables are
# small by design; the cap keeps a backlog (e.g. after a long outage) from
# turning one pass into a multi-minute transaction.
FIRE_LOG_PASS_BATCH = 50_000


@dataclass(frozen=True)
class _NoticeRow:
    id: int
    agent_id: int
    local_id: int
    title: str
    require_response: bool
    created_at: datetime
    expire_at: datetime
    replied: bool


def reap_expired_notices_blocking(
    pool: ConnectionPool,
    db: Database,
    bus: EventBus,
    *,
    zone: ZoneInfo | None,
    now: datetime,
) -> list[tuple[int, int, lifecycle.NoticeResolutionFacts]]:
    with write_transaction(pool) as conn:
        rows = query_all(
            conn,
            _NoticeRow,
            "SELECT id, agent_id, local_id, title, require_response, created_at, "
            "expire_at, (reply IS NOT NULL OR answer IS NOT NULL) AS replied "
            "FROM agent_notices WHERE expire_at <= now() AND resolved_at IS NULL "
            "ORDER BY expire_at, id LIMIT %s "
            "FOR UPDATE SKIP LOCKED",
            (PASS_BATCH,),
        )
        if not rows:
            return []
        reaped: list[tuple[int, int, lifecycle.NoticeResolutionFacts]] = []
        updated_agents: set[int] = set()
        queued: list[OwnerMessage] = []
        today = now.astimezone(zone).date()
        for notice in rows:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE agent_notices SET resolved_at = now(), resolution = 'expired' "
                    "WHERE id = %s AND resolved_at IS NULL",
                    (notice.id,),
                )
                if cur.rowcount == 0:
                    continue
            if notice.require_response:
                updated_agents.add(notice.agent_id)
            message = notify_owner(
                conn,
                notice.agent_id,
                owner_text.notice_expired_message(
                    notice.local_id,
                    notice.title,
                    owner_text.wall_clock(notice.expire_at, zone=zone, today=today),
                ),
                source="system:notice-expire",
                queue_when_terminated=True,
            )
            if message is not None:
                queued.append(message)
            reaped.append(
                (
                    notice.agent_id,
                    notice.id,
                    lifecycle.notice_resolution_facts(
                        "expired",
                        require_response=notice.require_response,
                        created_at=notice.created_at,
                        now=now,
                        replied=notice.replied,
                    ),
                )
            )
    wake_owners(db, bus, queued)
    for aid in updated_agents:
        publish_agent_updated_sync(bus, aid)
    return reaped


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
    db: Database,
    bus: EventBus,
    progress: LoopProgress,
    config: TtlReaperConfig,
) -> None:
    """One pass over every database-only phase, beating `progress` between them."""
    sessions = await asyncio.to_thread(reap_expired_web_sessions_blocking, pool)
    progress.beat()
    notices = await asyncio.to_thread(
        functools.partial(
            reap_expired_notices_blocking,
            pool,
            db,
            bus,
            zone=config.zone,
            now=config.clock.now(),
        )
    )
    for agent_id, nid, details in notices:
        await lifecycle.publish_notice_resolved(bus, agent_id, nid, facts=details)
    progress.beat()
    pruned_fire_log = await _slow_phases(pool, config)
    if sessions or notices or pruned_fire_log:
        _log.info(
            "[ttl-reaper] reclaimed %d web session(s), %d notice(s); "
            "pruned %d schedule fire-log row(s)",
            sessions,
            len(notices),
            pruned_fire_log,
        )


async def sweep_loop(
    pool: ConnectionPool,
    db: Database,
    bus: EventBus,
    progress: LoopProgress,
    config: TtlReaperConfig,
) -> None:
    """The database-only sweep as a resident sequential loop."""

    async def one_round() -> None:
        await sweep_round(pool, db, bus, progress, config)

    await round_loop.run_rounds("sweep", progress, config.poll_interval_s, one_round)

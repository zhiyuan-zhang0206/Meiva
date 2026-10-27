# meiva:note
# The reaper's remote-call loop.
#
# TTL-expired persistent shell sessions are killed on their home machines (`shells`), and the
# tracking rows of sessions a machine no longer lists are dropped (`shells` again). This loop dials
# other machines, so it runs apart from the database-only sweep (`sweep`): a slow or unreachable
# machine holds up this loop's next round, never a page expiry.

from __future__ import annotations

import logging

from psycopg_pool import ConnectionPool

from base.daemon import round_loop
from base.daemon.loop_health import LoopProgress
from base.db import Database
from base.events.live.bus import EventBus
from services.upkeep.ttl_reaper import shells
from services.upkeep.ttl_reaper.config import TtlReaperConfig

_log = logging.getLogger(__name__)


# meiva:note
# One pass: reclaim expired shells, then drop the tracking rows of sessions a machine no longer
# lists.
async def remote_round(
    pool: ConnectionPool,
    db: Database,
    bus: EventBus,
    progress: LoopProgress,
    config: TtlReaperConfig,
) -> None:
    reaped = await shells.reap_expired_shells(pool, db, bus, progress, config)
    progress.beat()
    if reaped:
        _log.info("[ttl-reaper] reclaimed %d shell(s)", len(reaped))
    dropped = await shells.reap_stale_shells(pool, db, progress, config)
    progress.beat()
    if dropped:
        _log.info("[ttl-reaper] dropped %d stale tracking row(s)", len(dropped))


async def remote_loop(
    pool: ConnectionPool,
    db: Database,
    bus: EventBus,
    progress: LoopProgress,
    config: TtlReaperConfig,
) -> None:
    """The remote-call phases as a resident sequential loop."""

    async def one_round() -> None:
        await remote_round(pool, db, bus, progress, config)

    await round_loop.run_rounds("remote", progress, config.poll_interval_s, one_round)

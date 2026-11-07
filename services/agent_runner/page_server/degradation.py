"""Page snapshots and terminal unavailable-link observation."""

from __future__ import annotations

from psycopg_pool import ConnectionPool

from base.events.live.bus import EventBus
from services.agent_runner.page_server.page_plan import PageRow


def close_failed_page(pool: ConnectionPool, row: PageRow, bus: EventBus) -> None:
    """Close only this snapshot; a fresh explicit registration stays untouched."""
    del pool, row, bus

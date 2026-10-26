"""Reads of the audit record — `audit_events` (Postgres) in the event stream's row shape.

`category=audit` events are recorded in Postgres (docs/decisions/data/database/2026-10-02-audit-events-in-postgres.md);
Loki holds only a projection that expires. Every reader that needs audit history reads here:
`/api/events`, the run timeline, the fleet graph and the neighbors walk. Rows come back in the
same dict shape `gateway.events.telemetry_rows.query_events` returns, so one merge serves both.

Row `id` is the event's stream id (the unsigned form of `audit_events.event_uid`), the same value
the Loki row of that event carries. `line_sha256` is the digest of the recorded row, not of a
Loki line: the table does not keep the line bytes.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from base.db.protocols import SyncConnection
from base.events.contract import EventTier
from base.events.reads import (
    EDGE_EVENT_NAMES as EDGE_EVENT_NAMES,
)
from base.events.reads import (
    LINEAGE_EVENT_NAMES as LINEAGE_EVENT_NAMES,
)
from base.events.reads import (
    AuditFilter,
    count_audit_rows,
    query_audit_rows,
)
from base.events.reads import (
    edge_counts as edge_counts,
)
from base.events.reads import (
    edge_weights as edge_weights,
)


def query_events(
    conn: SyncConnection,
    *,
    agent_id: int | None = None,
    involving_agents: list[int] | None = None,
    event_names: list[str] | None = None,
    tiers: list[EventTier] | None = None,
    trace_id: str | None = None,
    machine: str | None = None,
    level: str | None = None,
    attribute_filters: dict[str, str] | None = None,
    from_: datetime | None = None,
    to: datetime | None = None,
    limit: int = 100,
    offset: int = 0,
    direction: str = "backward",
) -> tuple[list[dict[str, Any]], bool]:
    return query_audit_rows(
        conn,
        AuditFilter(
            agent_id=agent_id,
            involving_agents=involving_agents,
            event_names=event_names,
            tiers=tiers,
            trace_id=trace_id,
            machine=machine,
            level=level,
            attribute_filters=attribute_filters,
            from_=from_,
            to=to,
        ),
        limit=limit,
        offset=offset,
        direction=direction,
    )


def count_events(
    conn: SyncConnection,
    *,
    agent_id: int | None = None,
    involving_agents: list[int] | None = None,
    event_names: list[str] | None = None,
    tiers: list[EventTier] | None = None,
    trace_id: str | None = None,
    machine: str | None = None,
    level: str | None = None,
    attribute_filters: dict[str, str] | None = None,
    from_: datetime | None = None,
    to: datetime | None = None,
) -> int:
    """Exact count of the audit rows `query_events` would page through."""
    return count_audit_rows(
        conn,
        AuditFilter(
            agent_id=agent_id,
            involving_agents=involving_agents,
            event_names=event_names,
            tiers=tiers,
            trace_id=trace_id,
            machine=machine,
            level=level,
            attribute_filters=attribute_filters,
            from_=from_,
            to=to,
        ),
    )

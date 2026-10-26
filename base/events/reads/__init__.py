from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any, LiteralString

from base.db.protocols import SyncConnection
from base.events.contract import EventTier

LINEAGE_EVENT_NAMES = ("spawn", "fork", "resurrect")
EDGE_EVENT_NAMES = ("send_message", *LINEAGE_EVENT_NAMES)

_ANOMALY_LEVELS = ("warning", "error", "critical")

_COLUMNS: LiteralString = (
    "event_uid, ts, trace_id, span_id, agent_id, machine, process, event_name, level, source, "
    "target_agent_id, attributes"
)


@dataclass(frozen=True)
class AuditFilter:
    agent_id: int | None = None
    involving_agents: Sequence[int] | None = None
    event_names: Sequence[str] | None = None
    tiers: Sequence[EventTier] | None = None
    trace_id: str | None = None
    machine: str | None = None
    level: str | None = None
    attribute_filters: Mapping[str, str] | None = None
    from_: datetime | None = None
    to: datetime | None = None


def _stream_id(event_uid: int) -> int:
    return event_uid + (1 << 64) if event_uid < 0 else event_uid


def _row(record: tuple[Any, ...]) -> dict[str, Any]:
    (
        event_uid,
        ts,
        trace_id,
        span_id,
        agent_id,
        machine,
        process,
        event_name,
        level,
        source,
        target_agent_id,
        attributes,
    ) = record
    ts = ts.astimezone(UTC)
    digest = json.dumps(
        [
            ts.isoformat(),
            trace_id,
            span_id,
            agent_id,
            machine,
            process,
            event_name,
            level,
            source,
            target_agent_id,
            attributes,
        ],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return {
        "id": _stream_id(event_uid),
        "line_sha256": sha256(digest.encode()).hexdigest(),
        "ts": ts,
        "trace_id": trace_id,
        "span_id": span_id,
        "agent_id": agent_id,
        "machine": machine,
        "process": process,
        "category": "audit",
        "event_name": event_name,
        "level": level,
        "source": source,
        "target_agent_id": target_agent_id,
        "attributes": attributes,
    }


def _tier_clause(tiers: Sequence[EventTier]) -> tuple[LiteralString | None, bool]:
    wants_business, wants_anomaly = "business" in tiers, "anomaly" in tiers
    if not (wants_business or wants_anomaly):
        return None, False
    if wants_business == wants_anomaly:
        return None, True
    return ("level = ANY(%s)" if wants_anomaly else "NOT (level = ANY(%s))"), True


def _where(filters: AuditFilter) -> tuple[LiteralString, list[Any]] | None:
    clauses: list[LiteralString] = []
    params: list[Any] = []

    def add(clause: LiteralString, *values: object) -> None:
        clauses.append(clause)
        params.extend(values)

    if filters.tiers is not None:
        tier_clause, possible = _tier_clause(filters.tiers)
        if not possible:
            return None
        if tier_clause is not None:
            add(tier_clause, list(_ANOMALY_LEVELS))
    if filters.involving_agents is not None:
        involved_list = list(filters.involving_agents)
        add(
            "(agent_id = ANY(%s) OR source = ANY(%s))",
            involved_list,
            [f"agent:{involved}" for involved in involved_list],
        )
    optional: tuple[tuple[LiteralString, Any], ...] = (
        ("agent_id = %s", filters.agent_id),
        (
            "event_name = ANY(%s)",
            list(filters.event_names) if filters.event_names is not None else None,
        ),
        ("trace_id = %s", filters.trace_id),
        ("machine = %s", filters.machine),
        ("level = %s", filters.level.lower() if filters.level is not None else None),
        ("ts >= %s", filters.from_),
        ("ts <= %s", filters.to),
    )
    for clause, value in optional:
        if value is not None:
            add(clause, value)
    for key, value in (filters.attribute_filters or {}).items():
        if value.startswith("!="):
            add("COALESCE(attributes ->> %s, '') <> %s", key, value[2:])
        else:
            add("COALESCE(attributes ->> %s, '') = %s", key, value)
    where: LiteralString = " AND ".join(clauses) if clauses else "TRUE"
    return where, params


def query_audit_rows(
    conn: SyncConnection,
    filters: AuditFilter,
    *,
    limit: int = 100,
    offset: int = 0,
    direction: str = "backward",
) -> tuple[list[dict[str, object]], bool]:
    where = _where(filters)
    if where is None:
        return [], False
    clause, params = where
    order: LiteralString = "DESC" if direction == "backward" else "ASC"
    # meiva:note
    # Both _COLUMNS and clause are LiteralString fragments assembled from fixed SQL templates; user values stay in params.
    query: LiteralString = (
        f"SELECT {_COLUMNS} FROM audit_events WHERE {clause} "  # noqa: S608
        f"ORDER BY ts {order}, id {order} LIMIT %s OFFSET %s"
    )
    records = conn.execute(query, [*params, limit + 1, offset]).fetchall()
    return [_row(record) for record in records[:limit]], len(records) > limit


def count_audit_rows(
    conn: SyncConnection,
    filters: AuditFilter,
) -> int:
    where = _where(filters)
    if where is None:
        return 0
    clause, params = where
    # meiva:note
    # clause is a LiteralString built from fixed SQL predicate templates; user filter values are passed via params.
    query: LiteralString = f"SELECT count(*) FROM audit_events WHERE {clause}"  # noqa: S608
    row = conn.execute(query, params).fetchone()
    assert row is not None  # noqa: S101
    return int(row[0])


def edge_weights(
    conn: SyncConnection,
    *,
    live_ids: set[int] | None,
    win_start: datetime | None,
    now: datetime,
    decay_lambda: float,
) -> list[tuple[int, int, str, float, int, datetime]]:
    query: LiteralString = """
        SELECT target_agent_id, agent_id, event_name,
               sum(CASE WHEN event_name = 'send_message'
                        THEN exp(-%(lam)s * extract(epoch FROM (%(now)s - ts)) / 86400.0)
                        ELSE 2.0 END)::float8,
               count(*), max(ts)
        FROM audit_events
        WHERE event_name = ANY(%(names)s)
          AND agent_id IS NOT NULL AND target_agent_id IS NOT NULL
          AND (event_name <> 'send_message' OR %(win)s::timestamptz IS NULL OR ts >= %(win)s)
          AND (%(live)s::bigint[] IS NULL
               OR (agent_id = ANY(%(live)s) AND target_agent_id = ANY(%(live)s)))
        GROUP BY target_agent_id, agent_id, event_name
    """
    records = conn.execute(
        query,
        {
            "lam": decay_lambda,
            "now": now,
            "names": list(EDGE_EVENT_NAMES),
            "win": win_start,
            "live": sorted(live_ids) if live_ids is not None else None,
        },
    ).fetchall()
    return [(int(t), int(a), str(n), float(w), int(c), last) for t, a, n, w, c, last in records]


def edge_counts(conn: SyncConnection) -> list[tuple[int, int, str, int, datetime]]:
    records = conn.execute(
        "SELECT agent_id, target_agent_id, event_name, count(*), max(ts) FROM audit_events "
        "WHERE event_name = ANY(%s) AND agent_id IS NOT NULL AND target_agent_id IS NOT NULL "
        "GROUP BY agent_id, target_agent_id, event_name",
        [list(EDGE_EVENT_NAMES)],
    ).fetchall()
    return [(int(a), int(t), str(n), int(c), last) for a, t, n, c, last in records]

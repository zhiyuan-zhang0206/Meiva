# meiva:note
# Rows in the two event stores, for tests that read them back through the gateway.

from __future__ import annotations

import uuid

import psycopg

__all__ = ["audit_event"]


# meiva:note
# Commit one `audit_events` row and return its event uid.
def audit_event(
    db: psycopg.Connection,
    name: str,
    *,
    minutes_ago: float,
    agent_id: int | None = 7,
    level: str = "info",
    machine: str = "m1",
    trace_id: str | None = None,
    attributes: str = "{}",
) -> int:
    uid = uuid.uuid4().int % (1 << 62)
    db.execute(
        "INSERT INTO audit_events (event_uid, ts, trace_id, agent_id, machine, process, "
        "event_name, level, source, attributes) "
        "VALUES (%s, now() - (%s * interval '1 minute'), %s, %s, %s, 'test', %s, %s, 'test', "
        "%s::jsonb)",
        (uid, minutes_ago, trace_id, agent_id, machine, name, level, attributes),
    )
    db.commit()
    return uid

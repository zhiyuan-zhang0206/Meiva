# meiva:note
# Rows in the two event stores, for tests that read them back through the gateway.

from __future__ import annotations

import json
import uuid
from typing import Any

import psycopg

__all__ = ["audit_event", "telemetry_event"]


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


# meiva:note
# Commit one `telemetry_events` row and return its event uid.
def telemetry_event(
    db: psycopg.Connection,
    name: str,
    *,
    minutes_ago: float,
    category: str = "telemetry",
    agent_id: int | None = 7,
    level: str = "info",
    machine: str = "m1",
    attributes: dict[str, Any] | None = None,
) -> int:
    uid = uuid.uuid4().int % (1 << 62)
    db.execute(
        "INSERT INTO telemetry_events (event_uid, ts, agent_id, machine, cluster, process, "
        "category, event_name, level, source, attributes) "
        "VALUES (%s, now() - (%s * interval '1 minute'), %s, %s, 'c', 'test', %s, %s, %s, "
        "'test', %s::jsonb)",
        (uid, minutes_ago, agent_id, machine, category, name, level, json.dumps(attributes or {})),
    )
    db.commit()
    return uid

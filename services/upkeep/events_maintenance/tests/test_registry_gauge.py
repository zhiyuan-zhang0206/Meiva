"""Agent-registry max-id gauge (task #2010), a loop of the events-maintenance service.

Locks the pieces the loop composes:

- ``read_max_agent_id_blocking`` — the exact SQL and the None guards (empty
  table / NULL row).
- ``emit_max_agent_id`` — one ``agent_registry`` telemetry event carrying the
  ``max_id`` payload (the OTLP side records it as a gauge via
  ``_METRIC_DISPOSITION``, locked in test_event_contract.py).

- ``registry_gauge_round`` / ``registry_gauge_loop`` — one sample per round, a
  sample at once, a crash ends the loop.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator

import psycopg
import pytest
from psycopg_pool import ConnectionPool

import base.db
from base.clock import SYSTEM_TIMERS
from base.daemon.loop_health import LoopProgress
from base.db import create_agent
from base.db.code_version_gate import ProcessDbGate
from base.telemetry.tests.capture.event_capture import EmittedEvents
from base.telemetry.tests.capture.event_capture import emitted_events as emitted_events
from services.upkeep.events_maintenance import registry_gauge
from services.upkeep.events_maintenance.tests.db_faults import table_missing


@pytest.fixture
def pool(db_url: str, *, database_gate: ProcessDbGate) -> Iterator[ConnectionPool]:
    p = base.db.pool(max_size=2, gate=database_gate)
    try:
        yield p
    finally:
        p.close()


def _empty_the_registry(conn: psycopg.Connection) -> None:
    conn.execute("TRUNCATE agents CASCADE")
    conn.commit()


def test_read_max_agent_id_is_the_registry_high_water_mark(
    db_conn: psycopg.Connection, pool: ConnectionPool
) -> None:
    create_agent(db_conn)
    newest = create_agent(db_conn)
    db_conn.commit()

    assert registry_gauge.read_max_agent_id_blocking(pool) == newest


# meiva:note
# The max over an empty table is NULL, never 0.
def test_read_max_agent_id_returns_none_for_an_empty_registry(
    db_conn: psycopg.Connection, pool: ConnectionPool
) -> None:
    _empty_the_registry(db_conn)

    assert registry_gauge.read_max_agent_id_blocking(pool) is None


def test_emit_max_agent_id_sends_one_telemetry_event(emitted_events: EmittedEvents) -> None:
    registry_gauge.emit_max_agent_id(9_999)
    assert [(e.category, e.event_name, e.attributes) for e in emitted_events] == [
        ("telemetry", "agent_registry", {"max_id": 9_999})
    ]


async def test_a_round_samples_once_and_emits(
    db_conn: psycopg.Connection, pool: ConnectionPool, emitted_events: EmittedEvents
) -> None:
    newest = create_agent(db_conn)
    db_conn.commit()

    await registry_gauge.registry_gauge_round(pool)

    assert [e.attributes["max_id"] for e in emitted_events.named("agent_registry")] == [newest]


async def test_a_round_over_an_empty_registry_emits_nothing(
    db_conn: psycopg.Connection, pool: ConnectionPool, emitted_events: EmittedEvents
) -> None:
    _empty_the_registry(db_conn)

    await registry_gauge.registry_gauge_round(pool)

    assert len(emitted_events) == 0


async def test_the_loop_samples_at_once_then_paces(
    db_conn: psycopg.Connection, pool: ConnectionPool, emitted_events: EmittedEvents
) -> None:
    newest = create_agent(db_conn)
    db_conn.commit()
    task = asyncio.create_task(
        registry_gauge.registry_gauge_loop(pool, LoopProgress("g", 180.0, timers=SYSTEM_TIMERS))
    )
    try:
        for _ in range(500):
            if len(emitted_events):
                break
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.2)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    # the sample at once; the 60 s wait never elapses
    assert [e.attributes["max_id"] for e in emitted_events] == [newest]


async def test_a_failing_sample_ends_the_loop(
    db_conn: psycopg.Connection, pool: ConnectionPool
) -> None:
    with table_missing(db_conn, "agents"), pytest.raises(psycopg.errors.UndefinedTable):
        await asyncio.wait_for(
            registry_gauge.registry_gauge_loop(
                pool, LoopProgress("g", 180.0, timers=SYSTEM_TIMERS)
            ),
            timeout=20.0,
        )

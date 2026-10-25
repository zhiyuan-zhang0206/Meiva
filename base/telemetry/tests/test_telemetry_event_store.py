from __future__ import annotations

import json
import socket
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import psycopg
import pytest

from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.db.config import DbConfig
from base.telemetry import event_store
from base.telemetry.emitter import Event, category_for_kind
from base.telemetry.tests.capture.event_capture import EmittedEvents
from base.telemetry.tests.capture.event_capture import emitted_events as emitted_events


@pytest.fixture
def _fresh_sink(db_url: str) -> Iterator[None]:
    event_store.set_enabled(enabled=True)
    event_store.reset_store()
    yield
    event_store.reset_store()
    event_store.set_enabled(enabled=False)


def _event(name: str, marker: str, **attributes: Any) -> Event:
    return Event(
        ts=datetime.now(UTC),
        trace_id=None,
        span_id=None,
        agent_id=None,
        machine=marker,
        cluster="store-cluster",
        process="store-proc",
        category=category_for_kind(name),
        event_name=name,
        level="info",
        source="system",
        target_agent_id=None,
        attributes=attributes,
    )


def _rows(db_conn: psycopg.Connection[Any], marker: str) -> list[tuple[Any, ...]]:
    return db_conn.execute(
        "SELECT event_name, category, cluster, attributes FROM telemetry_events "
        "WHERE machine = %s ORDER BY event_name",
        (marker,),
    ).fetchall()


def test_telemetry_and_log_events_land_and_audit_events_do_not(
    _fresh_sink: None,
    db_conn: psycopg.Connection[Any],
    database: Database,
) -> None:
    marker = uuid4().hex
    batch = [
        _event("llm_usage", marker),
        replace(_event("log", marker), level="error"),
        _event("spawn", marker),
    ]
    event_store.store_events(database, batch)
    event_store.store_events(database, batch)

    assert [(r[0], r[1], r[2]) for r in _rows(db_conn, marker)] == [
        ("llm_usage", "telemetry", "store-cluster"),
        ("log", "log", "store-cluster"),
    ]


def test_only_events_somebody_reads_land(
    _fresh_sink: None, db_conn: psycopg.Connection[Any], database: Database
) -> None:
    marker = uuid4().hex
    event_store.store_events(
        database,
        [
            _event("llm_usage", marker),
            _event("exec_envelope", marker),
            _event("log", marker),
            _event("never_registered_name", marker),
            replace(_event("log", marker), level="error"),
            replace(_event("exec_envelope", marker), level="warning"),
        ],
    )

    assert [(r[0], r[1]) for r in _rows(db_conn, marker)] == [
        ("exec_envelope", "telemetry"),
        ("llm_usage", "telemetry"),
        ("log", "log"),
        ("never_registered_name", "log"),
    ]
    assert [r[0] for r in _rows(db_conn, marker)].count("exec_envelope") == 1


@pytest.mark.parametrize(
    ("name", "level", "kept"),
    [
        ("llm_usage", "info", True),
        ("exec_envelope", "info", False),
        ("exec_envelope", "warning", True),
        ("exec_envelope", "WARNING", True),
        ("hook_timing", "debug", False),
        ("node_exit", "info", False),
        ("sdk_call", "info", False),
        ("log", "info", False),
        ("log", "critical", True),
        ("brand_new_unregistered", "info", True),
    ],
)
def test_is_persisted_is_the_one_judgment(name: str, level: str, kept: bool) -> None:
    assert event_store.is_persisted(name, level) is kept


def test_the_mirror_replay_applies_the_same_judgment() -> None:
    def line(name: str, level: str) -> bytes:
        row: dict[str, Any] = {
            "id": 1,
            "ts": "2026-10-01T00:00:00+00:00",
            "trace_id": None,
            "span_id": None,
            "agent_id": None,
            "machine": "m",
            "cluster": "c",
            "process": "p",
            "category": category_for_kind(name),
            "event_name": name,
            "level": level,
            "source": "system",
            "target_agent_id": None,
            "attributes": {},
        }
        return json.dumps(row).encode()

    records = event_store.jsonl_rows(
        [line("llm_usage", "info"), line("exec_envelope", "info"), line("exec_envelope", "error")]
    )

    assert [(r["event_name"], r["level"]) for r in records] == [
        ("llm_usage", "info"),
        ("exec_envelope", "error"),
    ]


def test_a_nul_character_is_replaced_not_fatal(
    _fresh_sink: None, db_conn: psycopg.Connection[Any], database: Database
) -> None:
    marker = uuid4().hex
    event_store.store_events(database, [_event("exec", marker, body="a\x00b")])

    [row] = _rows(db_conn, marker)
    assert row[3]["body"] == "a�b"


def _closed_local_port() -> int:
    with socket.create_server(("127.0.0.1", 0)) as sock:
        return int(sock.getsockname()[1])


def test_a_failed_batch_is_reported_once_then_backs_off_and_recovers(
    _fresh_sink: None,
    db_conn: psycopg.Connection[Any],
    database: Database,
    emitted_events: EmittedEvents,
) -> None:
    marker = uuid4().hex
    closed_port = _closed_local_port()
    unreachable_db = Database(
        DbConfig(
            db_url=f"postgresql://meiva:x@127.0.0.1:{closed_port}/meiva?connect_timeout=1",
            db_sslmode="",
            db_pool_min_size=1,
            db_pool_max_size=1,
            pgbouncer_enabled=False,
        ),
        gate=ProcessDbGate(process="test-unreachable", version=lambda: 1),
    )
    event_store.store_events(unreachable_db, [_event("llm_usage", marker)], now=100.0)
    # meiva:note
    # While inside the backoff window (100.5 < 102.0), even passing the live database skips dialing.
    event_store.store_events(database, [_event("llm_usage", marker)], now=100.5)

    failed_attrs = emitted_events.attributes("telemetry_store_failed")
    assert [(e["rows"], e["consecutive_failures"], e["error_class"]) for e in failed_attrs] == [
        (1, 1, "PoolTimeout")
    ]
    assert _rows(db_conn, marker) == []

    event_store.close_store()
    event_store.store_events(database, [_event("llm_usage", marker)], now=105.0)

    assert len(_rows(db_conn, marker)) == 1
    assert event_store.failures == 0


def test_a_process_without_database_authority_turns_the_sink_off(
    _fresh_sink: None,
    db_conn: psycopg.Connection[Any],
    database: Database,
) -> None:
    marker = uuid4().hex
    unadmitted_db = Database(
        DbConfig(
            db_url="postgresql://meiva@127.0.0.1:5432/meiva",
            db_sslmode="",
            db_pool_min_size=1,
            db_pool_max_size=1,
            pgbouncer_enabled=False,
            db_authority_refusal="no login for this home",
        ),
        gate=ProcessDbGate(process="test-unadmitted", version=lambda: 1),
    )
    event_store.store_events(unadmitted_db, [_event("llm_usage", marker)])
    assert event_store.set_enabled(enabled=False) is False
    # meiva:note
    # Subsequent calls are skipped even when given a valid database.
    event_store.store_events(database, [_event("llm_usage", marker)])

    assert _rows(db_conn, marker) == []

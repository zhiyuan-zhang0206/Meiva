"""Standalone SDK creation preserves its original accepted result."""

import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path
from threading import Barrier
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

import psycopg
import pytest
from psycopg import sql

import meiva
from base.agents.context import MeivaContext
from base.agents.context.clients import ClientSet
from base.agents.context.identity import AgentIdentity
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.events.live.tests.live_capture import recorded_events
from base.host.proc import run_bounded
from base.telemetry.delivery.serialization import event_line
from base.telemetry.tests.capture.event_capture import EmittedEvents
from base.telemetry.tests.capture.event_capture import emitted_events as emitted_events
from meiva.tests.fixtures.pin_agent import pin_agent
from meiva_builtins.plugins.meiva_fleet import _task_creation_receipts, task_registry
from meiva_builtins.plugins.meiva_fleet._task_requests import creation_request
from meiva_builtins.plugins.meiva_fleet.task_maintenance.tests.failing_writes import inserts_failing
from meiva_builtins.plugins.meiva_fleet.tests.task_registry.seeds import (
    root_task_id as root_task_id,
)
from meiva_builtins.plugins.meiva_fleet.tests.task_registry.seeds import (
    seed_agent,
)


def _facts(db: psycopg.Connection) -> tuple[object, ...]:
    return tuple(
        db.execute(sql.SQL("SELECT * FROM {} ORDER BY 1").format(sql.Identifier(table))).fetchall()
        for table in ("agent_tasks", "audit_events", "inbound_messages", "task_creation_receipts")
    )


def test_concurrent_creation_returns_same_original_task(
    db_conn: psycopg.Connection,
    root_task_id: int,
    pinned_bus: EventBus,
    *,
    database_gate: ProcessDbGate,
) -> None:
    actor, owner = seed_agent(db_conn), seed_agent(db_conn)
    barrier = Barrier(2)
    before = db_conn.execute("SELECT count(*) FROM inbound_messages").fetchone()

    def create(_index: int) -> task_registry.Task:
        clients = ClientSet(database=lambda: Database.from_settings(gate=database_gate))
        try:
            context = MeivaContext(
                identity=AgentIdentity(actor, True), clients=clients, bus=pinned_bus
            )
            barrier.wait()
            return task_registry.create_in_context(
                context,
                "concurrent creation",
                "work",
                parent=root_task_id,
                owner=owner,
                operation_key="same",
            )
        finally:
            clients.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        first, second = list(executor.map(create, range(2)))
    assert first == second
    assert db_conn.execute("SELECT count(*) FROM task_creation_receipts").fetchone() == (1,)
    assert db_conn.execute(
        "SELECT count(*) FROM audit_events WHERE event_name='task_create'"
    ).fetchone() == (1,)
    assert before is not None
    assert db_conn.execute("SELECT count(*) FROM inbound_messages").fetchone() == (before[0] + 1,)


def test_lost_response_returns_original_after_mutation_and_title_reuse(
    redis_url: str, db_conn: psycopg.Connection, root_task_id: int, emitted_events: EmittedEvents
) -> None:
    pin_agent(seed_agent(db_conn))
    committed = task_registry.commit_creation(
        meiva.context,
        "original",
        "work",
        parent=root_task_id,
        operation_key="lost",
        default_remind_interval_seconds=7200,
    )
    snapshot = db_conn.execute("SELECT result FROM task_creation_receipts").fetchone()
    assert snapshot is not None
    original = task_registry.Task(**snapshot[0])
    assert committed.task == original
    task_registry.update(original.id, title="renamed", status="done", operation_key=str(uuid4()))
    task_registry.create(
        "original", "another work", parent=root_task_id, operation_key=str(uuid4())
    )
    before = _facts(db_conn)
    emitted_events.clear()
    assert (
        task_registry.create("original", "work", parent=root_task_id, operation_key="lost")
        == original
    )
    assert _facts(db_conn) == before
    assert emitted_events.names() == []


def test_publication_emits_the_committed_events_once_and_in_order(
    pinned_bus: EventBus,
    db_conn: psycopg.Connection,
    root_task_id: int,
    emitted_events: EmittedEvents,
) -> None:
    actor, owner = seed_agent(db_conn), seed_agent(db_conn)
    pin_agent(actor)
    committed = task_registry.commit_creation(
        meiva.context,
        "published",
        "work",
        parent=root_task_id,
        owner=owner,
        operation_key=str(uuid4()),
        default_remind_interval_seconds=7200,
    )
    assert emitted_events.names() == []
    publication = committed.publication
    assert publication is not None
    assert task_registry.publication_events(publication)

    task_registry.publish_creation(publication, pinned_bus)

    assert emitted_events.names() == [
        event.event_name for event in task_registry.publication_events(publication)
    ]
    assert [event.attributes for event in emitted_events] == [
        event.attributes for event in task_registry.publication_events(publication)
    ]
    assert task_registry.publication_events(publication)[0].event_name == "task_create"
    assert (
        task_registry.publication_events(publication)[0].attributes["task_id"] == committed.task.id
    )


def test_deleted_task_and_parent_replay_snapshot(
    redis_url: str, db_conn: psycopg.Connection, root_task_id: int
) -> None:
    pin_agent(seed_agent(db_conn))
    parent = task_registry.create("parent", "work", parent=root_task_id, operation_key=str(uuid4()))
    original = task_registry.create("child", "work", parent=parent.id, operation_key="deleted")
    db_conn.execute("DELETE FROM agent_tasks WHERE id IN (%s,%s)", (original.id, parent.id))
    db_conn.commit()
    before = _facts(db_conn)
    assert (
        task_registry.create("child", "work", parent=parent.id, operation_key="deleted") == original
    )
    assert _facts(db_conn) == before


def test_default_intention_replays_original_and_owner_alias(
    redis_url: str, db_conn: psycopg.Connection, root_task_id: int
) -> None:
    actor = seed_agent(db_conn)
    pin_agent(actor)
    original = task_registry.create(
        "default", "work", parent=root_task_id, operation_key="defaults"
    )
    stored = db_conn.execute("SELECT request FROM task_creation_receipts").fetchone()
    assert stored == (
        creation_request(
            title="default",
            description="work",
            parent=root_task_id,
            owner=actor,
            priority=original.priority,
            remind_interval_seconds=None,
        ),
    )
    assert original.remind_interval_seconds is not None
    assert (
        task_registry.create(
            "default", "work", parent=root_task_id, owner=actor, operation_key="defaults"
        )
        == original
    )
    with pytest.raises(ValueError, match="different task creation"):
        task_registry.create(
            "default",
            "work",
            parent=root_task_id,
            remind_interval_seconds=original.remind_interval_seconds,
            operation_key="defaults",
        )


@pytest.mark.parametrize(
    "change", [{"description": "changed"}, {"parent": -1}, {"owner": -1}, {"priority": "P1"}]
)
def test_changed_request_conflicts_before_mutable_checks(
    redis_url: str, db_conn: psycopg.Connection, root_task_id: int, change: dict[str, Any]
) -> None:
    pin_agent(seed_agent(db_conn))
    kwargs: dict[str, Any] = {
        "title": "body",
        "description": "work",
        "parent": root_task_id,
        "operation_key": "body",
    }
    task_registry.create(**kwargs)
    before = _facts(db_conn)
    with pytest.raises(ValueError, match="different task creation"):
        task_registry.create(**(kwargs | change))
    assert _facts(db_conn) == before


@pytest.mark.parametrize(
    "table",
    [
        pytest.param("inbound_messages", id="notification"),
        pytest.param("task_creation_receipts", id="receipt"),
        pytest.param("audit_events", id="audit"),
    ],
)
def test_failure_after_effect_rolls_back_all_facts(
    db_conn: psycopg.Connection, root_task_id: int, table: str
) -> None:
    actor, owner = seed_agent(db_conn), seed_agent(db_conn)
    pin_agent(actor)
    before = _facts(db_conn)
    with (
        inserts_failing(db_conn, table),
        pytest.raises(psycopg.errors.RaiseException, match=f"{table} inserts refused"),
    ):
        task_registry.create(
            "rollback", "work", parent=root_task_id, owner=owner, operation_key="rollback"
        )
    assert _facts(db_conn) == before


@pytest.mark.parametrize("key", ["", "x" * 129, 12])
def test_invalid_key_has_no_effect(
    db_conn: psycopg.Connection, root_task_id: int, key: object
) -> None:
    pin_agent(seed_agent(db_conn))
    before = _facts(db_conn)
    with pytest.raises((ValueError, TypeError)):
        task_registry.create("invalid", "work", parent=root_task_id, operation_key=key)  # type: ignore[arg-type]
    assert _facts(db_conn) == before


# meiva:note
# A receipt written before `reminders_paused_until` existed still replays: the stored copy is upcast
# at the storage boundary, while a live snapshot that omits a field stays an error (task #5311).
def test_a_stored_receipt_without_the_pause_field_replays(
    db_conn: psycopg.Connection, root_task_id: int, pinned_bus: EventBus
) -> None:
    actor = seed_agent(db_conn)
    pin_agent(actor)
    key = str(uuid4())
    task_registry.create("replay", "work", parent=root_task_id, operation_key=key)
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE task_creation_receipts SET result = result - 'reminders_paused_until' "
            "WHERE actor_agent_id=%s AND operation_key=%s",
            (actor, key),
        )
        cur.execute(
            "SELECT request FROM task_creation_receipts "
            "WHERE actor_agent_id=%s AND operation_key=%s",
            (actor, key),
        )
        row = cur.fetchone()
    db_conn.commit()
    assert row is not None
    with db_conn.cursor() as cur:
        replayed = _task_creation_receipts.replay_creation(
            cur, actor, key, row[0], context=meiva.context
        )
    assert replayed is not None
    assert replayed["reminders_paused_until"] is None


def test_snapshot_validation_never_defaults_or_accepts_unknown_enum(
    redis_url: str, db_conn: psycopg.Connection, root_task_id: int
) -> None:
    pin_agent(seed_agent(db_conn))
    snapshot: dict[str, object] = asdict(
        task_registry.create("snapshot", "work", parent=root_task_id, operation_key=str(uuid4()))
    )
    for invalid in (
        {k: v for k, v in snapshot.items() if k != "priority"},
        snapshot | {"unknown": 1},
        snapshot | {"status": "unknown"},
        snapshot | {"priority": "unknown"},
        snapshot | {"id": "1"},
    ):
        with pytest.raises(ValueError):
            _task_creation_receipts.validate_snapshot(invalid)


def test_actor_scope_independent_and_new_key_keeps_title_constraint(
    redis_url: str, db_conn: psycopg.Connection, root_task_id: int
) -> None:
    first_actor, second_actor = seed_agent(db_conn), seed_agent(db_conn)
    pin_agent(first_actor)
    original = task_registry.create("first", "work", parent=root_task_id, operation_key="same")
    with pytest.raises(ValueError, match="duplicate in_progress"):
        task_registry.create("first", "work", parent=root_task_id, operation_key="new")
    pin_agent(second_actor)
    other = task_registry.create("second", "work", parent=root_task_id, operation_key="same")
    assert original.id != other.id
    assert db_conn.execute("SELECT count(*) FROM task_creation_receipts").fetchone() == (2,)


# meiva:note
# A caller may edit the mutable SDK result or an Event it materialized, but the publication
# still names and emits exactly the facts that committed. The board signal crosses real Redis.
def test_sdk_result_and_materialized_events_cannot_rewrite_publication(
    db_conn: psycopg.Connection,
    root_task_id: int,
    event_bus: EventBus,
    emitted_events: EmittedEvents,
) -> None:
    pin_agent(seed_agent(db_conn))
    committed = task_registry.commit_creation(
        meiva.context,
        "immutable publication",
        "work",
        parent=root_task_id,
        operation_key="immutable",
        default_remind_interval_seconds=7200,
    )
    publication = committed.publication
    assert publication is not None
    task_id = committed.task.id
    retained_lines = publication.event_lines
    committed.task.id = -99
    materialized = task_registry.publication_events(publication)
    materialized[0].attributes["task_id"] = -99
    materialized[0].attributes["unexpected"] = ["caller data"]
    assert publication.event_lines == retained_lines
    assert (
        tuple(event_line(event) for event in task_registry.publication_events(publication))
        == retained_lines
    )

    with recorded_events(event_bus) as messages:
        task_registry.publish_creation(publication, event_bus)
    assert [message["task_id"] for message in messages] == [task_id]
    assert tuple(event_line(event) for event in emitted_events) == retained_lines


# meiva:note
# A receipt replays the accepted SDK snapshot before today's defaults or timestamp renderer
# become effects. The fresh operation proves the second process actually uses its new inputs.
def test_receipt_replays_before_new_default_and_timezone_resolution(
    db_conn: psycopg.Connection, root_task_id: int
) -> None:
    actor = seed_agent(db_conn)

    def commit(zone: str, interval: int, key: str) -> dict[str, Any]:
        result = run_bounded(
            [
                sys.executable,
                "-I",
                str(Path(__file__).with_name("child_creation.py")),
                str(root_task_id),
                str(interval),
                key,
            ],
            env=dict(os.environ)
            | {"MEIVA_AGENT_ID": str(actor), "MEIVA_TIMEZONE": zone, "MEIVA_CONFIG_FETCH": "skip"},
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout.strip().splitlines()[-1])

    original = commit("UTC", 7200, "replayed")
    before = _facts(db_conn)
    replay = commit("Asia/Tokyo", 777, "replayed")
    assert replay == original | {"publish": False}
    assert _facts(db_conn) == before
    fresh = commit("Asia/Tokyo", 777, "fresh")
    assert original["task"]["remind_interval_seconds"] == 7200
    assert fresh["task"]["remind_interval_seconds"] == 777
    assert fresh["publish"] is True
    for result, zone in ((original, "UTC"), (fresh, "Asia/Tokyo")):
        row = db_conn.execute(
            "SELECT created_at FROM agent_tasks WHERE id=%s", (result["task"]["id"],)
        ).fetchone()
        assert row is not None
        local = row[0].astimezone(ZoneInfo(zone))
        assert result["task"]["created_at"].startswith(local.strftime("[%Y-%m-%d "))
        assert result["task"]["created_at"].endswith(local.strftime("%H:%M:%S]"))

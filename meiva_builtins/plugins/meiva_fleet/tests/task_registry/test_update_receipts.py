"""Real SDK mutations commit once per actor, task and caller operation key."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import psycopg
import pytest

import meiva
from base.agents.context import MeivaContext
from base.agents.context.clients import ClientSet
from base.agents.context.identity import AgentIdentity
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.paths import meiva_home
from base.telemetry.tests.capture.event_capture import EmittedEvents
from base.telemetry.tests.capture.event_capture import emitted_events as emitted_events
from meiva.tests.fixtures.pin_agent import pin_agent
from meiva_builtins.plugins.meiva_fleet import task_registry
from meiva_builtins.plugins.meiva_fleet.task_maintenance.tests.failing_writes import inserts_failing
from meiva_builtins.plugins.meiva_fleet.tests.task_registry.seeds import (
    root_task_id as root_task_id,
)
from meiva_builtins.plugins.meiva_fleet.tests.task_registry.seeds import (
    seed_agent,
    seed_owned_task,
)


def _facts(db: psycopg.Connection, tid: int) -> tuple[object, object, object]:
    task = db.execute(
        "SELECT owner, results, updated_at, reminder_count, last_reminded_at, escalated_at "
        "FROM agent_tasks WHERE id=%s",
        (tid,),
    ).fetchone()
    audit = db.execute("SELECT id, attributes FROM audit_events ORDER BY id").fetchall()
    notes = db.execute("SELECT id, status, payload FROM inbound_messages ORDER BY id").fetchall()
    return task, audit, notes


def test_concurrent_duplicate_note_has_one_business_effect(
    db_conn: psycopg.Connection,
    root_task_id: int,
    pinned_bus: EventBus,
    *,
    database_gate: ProcessDbGate,
) -> None:
    actor, _owner, tid = seed_owned_task(db_conn, root_task_id)
    before_notes = db_conn.execute("SELECT count(*) FROM inbound_messages").fetchone()
    barrier = Barrier(2)

    def append(_index: int) -> None:
        clients = ClientSet(database=lambda: Database.from_settings(gate=database_gate))
        try:
            context = MeivaContext(
                identity=AgentIdentity(actor, True),
                clients=clients,
                bus=pinned_bus,
                meiva_home=meiva_home(),
            )
            barrier.wait()
            task_registry.update_in_context(
                context, tid, note="one progress line", operation_key="same"
            )
        finally:
            clients.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(append, range(2)))
    result = task_registry.get(tid).results
    assert result is not None and result.count("one progress line") == 1
    assert db_conn.execute("SELECT count(*) FROM task_update_receipts").fetchone() == (1,)
    assert before_notes is not None
    assert db_conn.execute(
        "SELECT count(*) FROM audit_events WHERE event_name='task_update'"
    ).fetchone() == (1,)
    assert db_conn.execute("SELECT count(*) FROM inbound_messages").fetchone() == (
        before_notes[0] + 1,
    )


def test_post_commit_failure_replay_preserves_intervening_state(
    redis_url: str, db_conn: psycopg.Connection, root_task_id: int, emitted_events: EmittedEvents
) -> None:
    _actor, _owner, tid = seed_owned_task(db_conn, root_task_id)
    task_registry.commit_update(meiva.context, tid, note="committed", operation_key="lost")
    current = seed_agent(db_conn)
    task_registry.update(tid, owner=current, operation_key=str(uuid4()))
    db_conn.execute(
        "UPDATE agent_tasks SET reminder_count=4, escalated_at=now() WHERE id=%s", (tid,)
    )
    db_conn.commit()
    before = _facts(db_conn, tid)
    emitted_events.clear()
    assert task_registry.update(tid, note="committed", operation_key="lost") is None
    assert _facts(db_conn, tid) == before
    assert emitted_events.names() == []


def test_publication_emits_the_committed_events_once_and_in_order(
    pinned_bus: EventBus,
    db_conn: psycopg.Connection,
    root_task_id: int,
    emitted_events: EmittedEvents,
) -> None:
    _actor, _owner, tid = seed_owned_task(db_conn, root_task_id)
    emitted_events.clear()
    committed = task_registry.commit_update(
        meiva.context, tid, note="published", operation_key=str(uuid4())
    )
    assert emitted_events.names() == []
    assert committed is not None
    assert task_registry.publication_events(committed)[0].event_name == "task_update"

    task_registry.publish_update(committed, pinned_bus)

    assert emitted_events.names() == [
        event.event_name for event in task_registry.publication_events(committed)
    ]
    assert [event.attributes for event in emitted_events] == [
        event.attributes for event in task_registry.publication_events(committed)
    ]


@pytest.mark.parametrize(
    "table",
    [
        pytest.param("inbound_messages", id="notification"),
        pytest.param("task_update_receipts", id="receipt"),
        pytest.param("audit_events", id="audit"),
    ],
)
def test_producer_failure_rolls_back_entire_operation(
    redis_url: str, db_conn: psycopg.Connection, root_task_id: int, table: str
) -> None:
    _actor, _owner, tid = seed_owned_task(db_conn, root_task_id)
    before = _facts(db_conn, tid)
    with (
        inserts_failing(db_conn, table),
        pytest.raises(psycopg.errors.RaiseException, match=f"{table} inserts refused"),
    ):
        task_registry.log(tid, "rolled back", operation_key="rollback")
    assert _facts(db_conn, tid) == before
    assert db_conn.execute("SELECT count(*) FROM task_update_receipts").fetchone() == (0,)


def test_effective_body_aliases_and_conflicts(
    redis_url: str, db_conn: psycopg.Connection, root_task_id: int
) -> None:
    _actor, _owner, tid = seed_owned_task(db_conn, root_task_id)
    task_registry.log(tid, "", operation_key="body")
    before = _facts(db_conn, tid)
    task_registry.update(
        tid, note="", owner=None, remind_interval_seconds=None, operation_key="body"
    )
    assert _facts(db_conn, tid) == before
    for kwargs in (
        {"parent_id": None, "note": ""},
        {"note": "changed"},
        {"priority": "P1", "note": ""},
    ):
        with pytest.raises(ValueError, match="different task update"):
            task_registry.update(tid, operation_key="body", **kwargs)  # type: ignore[arg-type]
    assert _facts(db_conn, tid) == before


def test_actor_and_task_scopes_are_independent(
    redis_url: str, db_conn: psycopg.Connection, root_task_id: int
) -> None:
    actor, owner, tid = seed_owned_task(db_conn, root_task_id)
    second = task_registry.create("other", "work", parent=root_task_id, operation_key=str(uuid4()))
    for who, target in ((actor, tid), (actor, second.id), (owner, tid)):
        pin_agent(who)
        task_registry.log(target, "separate", operation_key="same raw key")
    assert db_conn.execute("SELECT count(*) FROM task_update_receipts").fetchone() == (3,)


def test_retained_void_receipt_survives_task_deletion(
    redis_url: str, db_conn: psycopg.Connection, root_task_id: int
) -> None:
    _actor, _owner, tid = seed_owned_task(db_conn, root_task_id)
    task_registry.update(tid, priority="P1", operation_key="deleted")
    db_conn.execute("DELETE FROM agent_tasks WHERE id=%s", (tid,))
    db_conn.commit()
    assert task_registry.update(tid, priority="P1", operation_key="deleted") is None
    assert db_conn.execute("SELECT count(*) FROM task_update_receipts").fetchone() == (1,)

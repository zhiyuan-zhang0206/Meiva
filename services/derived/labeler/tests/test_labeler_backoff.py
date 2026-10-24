from __future__ import annotations

import asyncio
from typing import Any

import psycopg
import pytest

from base.daemon.health import Liveness
from base.db import create_agent, pool
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.lm.catalog import ModelCatalog
from services.derived.labeler import daemon
from services.derived.labeler.tests.slices import labeler_config, labeler_db, model_inputs


def _seed_chat(db: psycopg.Connection, tid: int, content: str = "do the thing") -> None:
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO inbound_messages (agent_id, content, kind, source) "
            "VALUES (%s, %s, 'chat', 'user')",
            (tid, content),
        )


def _label(db: psycopg.Connection, tid: int) -> str | None:
    db.commit()
    with db.cursor() as cur:
        cur.execute("SELECT label FROM agents WHERE id = %s", (tid,))
        row = cur.fetchone()
    assert row is not None
    return row[0]


async def _until(condition: Any, *, what: str, timeout_s: float = 15.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout_s
    while not condition():
        if asyncio.get_running_loop().time() > deadline:
            pytest.fail(f"timed out waiting for {what}")
        await asyncio.sleep(0.02)


async def test_dispatch_loop_labels_an_unlabeled_agent_with_the_configured_model(
    db_conn: psycopg.Connection,
    event_bus: EventBus,
    model_catalog: ModelCatalog,
    *,
    database_gate: ProcessDbGate,
) -> None:
    a = create_agent(db_conn)
    _seed_chat(db_conn, a)
    db_conn.commit()
    p = pool(gate=database_gate)
    loop = asyncio.create_task(
        daemon.dispatch_loop(
            p,
            labeler_db(database_gate=database_gate),
            event_bus,
            Liveness(daemon.LIVENESS_TIMEOUT_S),
            labeler_config("say:Label from the labeler model"),
            **model_inputs(model_catalog),
        )
    )
    try:
        await _until(lambda: _label(db_conn, a) is not None, what="the label")
    finally:
        loop.cancel()
        await asyncio.gather(loop, return_exceptions=True)
        p.close()

    assert _label(db_conn, a) == "Label from the labeler model"


def test_select_unlabeled_excludes_attempted_ids(db_conn: psycopg.Connection) -> None:
    a = create_agent(db_conn)
    b = create_agent(db_conn)
    _seed_chat(db_conn, a)
    _seed_chat(db_conn, b)

    with db_conn.cursor() as cur:
        all_ids = {tid for tid, _prompt in daemon.select_unlabeled(cur, [])}
        assert {a, b} <= all_ids

        remaining = {tid for tid, _prompt in daemon.select_unlabeled(cur, [a])}
        assert a not in remaining
        assert b in remaining


def _set_label(db: psycopg.Connection, tid: int, label: str) -> None:
    with db.cursor() as cur:
        cur.execute("UPDATE agents SET label = %s WHERE id = %s", (label, tid))


def _seed_task_note(db: psycopg.Connection, tid: int, content: str) -> None:
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO inbound_messages (agent_id, content, kind, source, payload) "
            "VALUES (%s, %s, 'system_note', 'agent:405', %s::jsonb)",
            (tid, content, '{"note_tag": "task"}'),
        )


def test_select_unlabeled_treats_empty_label_as_unset(db_conn: psycopg.Connection) -> None:
    a = create_agent(db_conn)
    _set_label(db_conn, a, "")
    _seed_chat(db_conn, a)
    b = create_agent(db_conn)
    _set_label(db_conn, b, "")
    c = create_agent(db_conn)
    _seed_chat(db_conn, c)
    _set_label(db_conn, c, "already labeled")

    with db_conn.cursor() as cur:
        picked = {tid for tid, _prompt in daemon.select_unlabeled(cur, [])}
    assert a in picked
    assert b not in picked
    assert c not in picked


def test_select_unlabeled_prompts_from_task_system_note(db_conn: psycopg.Connection) -> None:
    a = create_agent(db_conn)
    _seed_task_note(db_conn, a, 'Task #2669 "meiva.ui docs gap" is now assigned to you.')
    b = create_agent(db_conn)
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO inbound_messages (agent_id, content, kind, source, payload) "
            "VALUES (%s, %s, 'system_note', 'system', %s::jsonb)",
            (b, "impersonation rolled back", '{"impersonation_id": "x"}'),
        )
    db_conn.commit()

    with db_conn.cursor() as cur:
        rows = dict(daemon.select_unlabeled(cur, []))
    assert rows[a] == 'Task #2669 "meiva.ui docs gap" is now assigned to you.'
    assert b not in rows


# meiva:note
# The first agent's request makes the provider fail, which leaves its label unset. The
# loop must not attempt it again while the polls go on, and must still label a new agent.
async def test_failed_label_is_not_retried_but_new_agents_are_processed(
    db_conn: psycopg.Connection,
    event_bus: EventBus,
    model_catalog: ModelCatalog,
    loguru_records: list[dict[str, Any]],
    *,
    database_gate: ProcessDbGate,
) -> None:
    failed = create_agent(db_conn)
    _seed_chat(db_conn, failed, "FAIL this one")
    db_conn.commit()

    def failures_of(agent_id: int) -> int:
        return sum(
            1
            for record in loguru_records
            if record["extra"].get("event") == "label_generate_failed"
            and record["extra"].get("agent_id") == agent_id
        )

    p = pool(gate=database_gate)
    loop = asyncio.create_task(
        daemon.dispatch_loop(
            p,
            labeler_db(database_gate=database_gate),
            event_bus,
            Liveness(120),
            labeler_config("failif:FAIL:rate-limit"),
            **model_inputs(model_catalog),
        )
    )
    try:
        await _until(lambda: failures_of(failed) == 1, what="the first failed attempt")
        fresh = create_agent(db_conn)
        _seed_chat(db_conn, fresh)
        db_conn.commit()
        await _until(lambda: _label(db_conn, fresh) is not None, what="the new agent's label")
        await asyncio.sleep(0.3)
    finally:
        loop.cancel()
        await asyncio.gather(loop, return_exceptions=True)
        p.close()

    assert failures_of(failed) == 1
    assert _label(db_conn, failed) is None

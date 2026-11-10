# meiva:note
# The agent's own restart on its home runner (`at_turn_end`): it is recorded as one pending
# `restart` command carrying the transition and changes nothing else, so a harness switch never
# touches an agent that is still working. The host applies the command when the turn ends
# (`agent/ownership/tests/test_restart_transition_apply.py`).
# meiva:link ops/lifecycle/deferred_restart.py

from __future__ import annotations

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from base.agents.incarnation.restart_transition import RestartTransition, restart_transition_of
from base.db import Database
from base.events.live.bus import EventBus
from base.lm.catalog import ModelCatalog
from base.paths import resolve_meiva_home
from ops import lifecycle


def _set(db_conn: psycopg.Connection, agent_id: int, *, status: str, harness: str) -> None:
    db_conn.execute(
        "UPDATE agents_meta SET status = %s, harness = %s WHERE id = %s",
        (status, harness, agent_id),
    )
    db_conn.commit()


async def _restart(
    agent_id: int,
    body: dict[str, object],
    *,
    database: Database,
    event_bus: EventBus,
    db_pool: ConnectionPool,
    model_catalog: ModelCatalog,
) -> str:
    result = await lifecycle.lifecycle_op(
        database,
        event_bus,
        f"/api/agents/{agent_id}/restart",
        {"source": f"agent:{agent_id}", "at_turn_end": True, **body},
        db_pool,
        catalog=model_catalog,
        agent_host_port=8114,
        home=resolve_meiva_home(),
    )
    return str(result.status)


def _commands(db_conn: psycopg.Connection, agent_id: int) -> list[tuple[object, ...]]:
    db_conn.commit()
    return db_conn.execute(
        "SELECT kind, source, content, status, payload FROM inbound_messages WHERE agent_id=%s",
        (agent_id,),
    ).fetchall()


def _meta(db_conn: psycopg.Connection, agent_id: int) -> tuple[object, ...]:
    db_conn.commit()
    row = db_conn.execute(
        "SELECT status, harness, config_overlay FROM agents_meta WHERE id=%s", (agent_id,)
    ).fetchone()
    assert row is not None
    return row


@pytest.mark.parametrize(
    ("status", "stored", "overlay", "target", "kept"),
    [
        ("running", "meiva", {"harness": "codex", "llm_model": "gpt-6.1-sol"}, "codex", {}),
        ("running", "codex", {"harness": "meiva", "llm_model": "deepseek-flash"}, "meiva", {}),
        ("running", "codex", {"exec_timeout_seconds": 30}, "codex", {"exec_timeout_seconds": 30}),
        ("idling", "claude_code", None, "claude_code", {}),
    ],
    ids=["native-to-external", "external-to-native", "external-same-harness", "idle-caller"],
)
async def test_the_request_is_one_pending_command_and_nothing_else_changes(
    lifecycle_agent_id: int,
    db_conn: psycopg.Connection,
    db_pool: ConnectionPool,
    database: Database,
    event_bus: EventBus,
    model_catalog: ModelCatalog,
    status: str,
    stored: str,
    overlay: dict[str, object] | None,
    target: str,
    kept: dict[str, object],
) -> None:
    _set(db_conn, lifecycle_agent_id, status=status, harness=stored)
    before = _meta(db_conn, lifecycle_agent_id)

    answer = await _restart(
        lifecycle_agent_id,
        {"message": "switching", **({} if overlay is None else {"config_overlay": overlay})},
        database=database,
        event_bus=event_bus,
        db_pool=db_pool,
        model_catalog=model_catalog,
    )

    assert answer == "enqueued"
    [(kind, source, content, state, payload)] = _commands(db_conn, lifecycle_agent_id)
    assert (kind, source, content, state) == ("restart", "self", "switching", "pending")
    expected = {k: v for k, v in (overlay or {}).items() if k != "harness"}
    assert restart_transition_of(payload) == RestartTransition(target, expected)
    assert expected.keys() >= kept.keys()
    assert _meta(db_conn, lifecycle_agent_id) == before


async def test_a_second_request_while_one_waits_is_refused_and_names_the_first(
    lifecycle_agent_id: int,
    db_conn: psycopg.Connection,
    db_pool: ConnectionPool,
    database: Database,
    event_bus: EventBus,
    model_catalog: ModelCatalog,
) -> None:
    _set(db_conn, lifecycle_agent_id, status="running", harness="codex")
    _ = await _restart(
        lifecycle_agent_id,
        {"message": "first"},
        database=database,
        event_bus=event_bus,
        db_pool=db_pool,
        model_catalog=model_catalog,
    )
    [(first, *_rest)] = db_conn.execute(
        "SELECT id FROM inbound_messages WHERE agent_id=%s", (lifecycle_agent_id,)
    ).fetchall()

    with pytest.raises(ValueError, match=rf"restart command {first} .* already waiting"):
        _ = await _restart(
            lifecycle_agent_id,
            {"message": "second"},
            database=database,
            event_bus=event_bus,
            db_pool=db_pool,
            model_catalog=model_catalog,
        )

    assert len(_commands(db_conn, lifecycle_agent_id)) == 1


async def test_a_terminated_agent_cannot_queue_a_restart(
    lifecycle_agent_id: int,
    db_conn: psycopg.Connection,
    db_pool: ConnectionPool,
    database: Database,
    event_bus: EventBus,
    model_catalog: ModelCatalog,
) -> None:
    _set(db_conn, lifecycle_agent_id, status="terminated", harness="codex")

    with pytest.raises(ValueError, match="status 'terminated'"):
        _ = await _restart(
            lifecycle_agent_id,
            {"message": "too late"},
            database=database,
            event_bus=event_bus,
            db_pool=db_pool,
            model_catalog=model_catalog,
        )

    assert _commands(db_conn, lifecycle_agent_id) == []

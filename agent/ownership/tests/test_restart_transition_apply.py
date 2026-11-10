# meiva:note
# The host's apply of a restart command that carries a transition (the agent's own restart, applied
# when its turn ended), on real Postgres: the harness, the overlay, the released incarnation, the
# closed command and the note the agent wakes to are one commit, and a command is applied once.
# meiva:link agent/graph/docs/turn-interrupts.md

import json
from uuid import uuid4

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from agent.ownership.hosted import (
    HarnessSwitch,
    admit_hosted_runtime,
    apply_hosted_lifecycle,
    claimed_harness_switch_target,
)
from base.agents.incarnation.restart_transition import RESTART_TRANSITION, RestartTransition
from base.db import Database, create_agent
from base.native_process.runtime_incarnation import RuntimeIncarnation


def _agent(conn: psycopg.Connection, *, harness: str = "meiva") -> int:
    agent_id = create_agent(conn)
    conn.execute(
        "INSERT INTO agents_meta (id, status, machine, harness) "
        "VALUES (%s, 'idling', 'host-test', %s) "
        "ON CONFLICT (id) DO UPDATE SET status = 'idling', machine = 'host-test', harness = %s",
        (agent_id, harness, harness),
    )
    conn.commit()
    return agent_id


def _claim_restart(conn: psycopg.Connection, agent_id: int, message: str, payload: object) -> int:
    row = conn.execute(
        "INSERT INTO inbound_messages (agent_id, content, kind, source, status, payload) "
        "VALUES (%s, %s, 'restart', 'self', 'claimed', %s::jsonb) RETURNING id",
        (agent_id, message, json.dumps(payload)),
    ).fetchone()
    conn.commit()
    match row:
        case (int() as command,):
            return command
        case _:
            raise AssertionError(f"the insert returned {row!r}, not an id")


async def _admitted(
    conn: psycopg.Connection, pool: AsyncConnectionPool, database: Database, agent_id: int
) -> RuntimeIncarnation:
    incarnation = await admit_hosted_runtime(pool, agent_id, "host-test", uuid4(), db=database)
    assert incarnation is not None
    return incarnation


def _meta(conn: psycopg.Connection, agent_id: int) -> tuple[object, ...]:
    conn.commit()
    row = conn.execute(
        "SELECT status, runtime_owner, harness, config_overlay, "
        "birth_config -> 'harness_model_selection', harness_configured_at IS NOT NULL "
        "FROM agents_meta WHERE id = %s",
        (agent_id,),
    ).fetchone()
    assert row is not None
    return row


def _notes(conn: psycopg.Connection, agent_id: int) -> list[tuple[object, ...]]:
    conn.commit()
    return conn.execute(
        "SELECT content, source, status FROM inbound_messages "
        "WHERE agent_id = %s AND kind = 'system_note'",
        (agent_id,),
    ).fetchall()


async def test_a_switch_is_one_commit_and_is_applied_once(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool, database: Database
) -> None:
    agent_id = _agent(db_conn)
    incarnation = await _admitted(db_conn, aops_pool, database, agent_id)
    command = _claim_restart(
        db_conn,
        agent_id,
        "I need a real shell",
        RestartTransition("codex", {"llm_model": "gpt-6.1-sol"}).payload(),
    )
    assert await claimed_harness_switch_target(aops_pool, incarnation) == "codex"

    applied = await apply_hosted_lifecycle(aops_pool, incarnation, resume_turn=True)

    assert applied is not None and applied.kind == "restart"
    assert applied.switch == HarnessSwitch("meiva", "codex", "self", "I need a real shell")
    status, owner, harness, overlay, selection, configured = _meta(db_conn, agent_id)
    assert (status, owner, harness, selection, configured) == ("idling", None, "codex", 1, True)
    assert isinstance(overlay, dict) and overlay["llm_model"] == "gpt-6.1-sol"
    assert _notes(db_conn, agent_id) == [
        ("[Harness switched from 'meiva' to 'codex']\nI need a real shell", "self", "pending")
    ]
    done = db_conn.execute(
        "SELECT status, payload -> 'lifecycle_release' FROM inbound_messages WHERE id = %s",
        (command,),
    ).fetchone()
    assert done == ("done", True)

    # meiva:note
    # A retry after the commit finds nothing claimed: no second note, no second switch.
    assert await apply_hosted_lifecycle(aops_pool, incarnation, resume_turn=True) is None
    assert len(_notes(db_conn, agent_id)) == 1
    assert await claimed_harness_switch_target(aops_pool, incarnation) is None


async def test_a_restart_that_keeps_its_harness_stores_the_overlay_and_reports_no_switch(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool, database: Database
) -> None:
    agent_id = _agent(db_conn, harness="codex")
    incarnation = await _admitted(db_conn, aops_pool, database, agent_id)
    _ = _claim_restart(
        db_conn,
        agent_id,
        "shorter timeout",
        RestartTransition("codex", {"exec_timeout_seconds": 30}).payload(),
    )
    assert await claimed_harness_switch_target(aops_pool, incarnation) is None

    applied = await apply_hosted_lifecycle(aops_pool, incarnation, resume_turn=True)

    assert applied is not None and applied.switch is None
    status, owner, harness, overlay, selection, _configured = _meta(db_conn, agent_id)
    assert (status, owner, harness, selection) == ("idling", None, "codex", None)
    assert isinstance(overlay, dict) and overlay["exec_timeout_seconds"] == 30
    assert _notes(db_conn, agent_id) == [
        ("Restart was accepted from yourself: shorter timeout", "self", "pending")
    ]


async def test_a_malformed_transition_changes_nothing(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool, database: Database
) -> None:
    agent_id = _agent(db_conn)
    incarnation = await _admitted(db_conn, aops_pool, database, agent_id)
    _ = _claim_restart(db_conn, agent_id, "bad", {RESTART_TRANSITION: {"target_harness": "codex"}})

    with pytest.raises(TypeError, match=RESTART_TRANSITION):
        _ = await apply_hosted_lifecycle(aops_pool, incarnation, resume_turn=True)

    status, owner, harness, *_ = _meta(db_conn, agent_id)
    assert (status, harness) == ("running", "meiva")
    assert owner is not None
    assert _notes(db_conn, agent_id) == []

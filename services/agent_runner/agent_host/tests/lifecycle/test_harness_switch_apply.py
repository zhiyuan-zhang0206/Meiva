# meiva:note
# The host's apply of the agent's own restart that carries a harness transition, on real Postgres
# and a real session registry directory. A switch removes the old harness's session record before
# the commit (so the new harness cannot resume it) and the command stays claimed until the commit,
# so a host that dies in between leaves a command its successor claims again: every effect before
# the commit can run twice. A restart that keeps its harness keeps the session.
# meiva:link agent/graph/docs/turn-interrupts.md

import json
from pathlib import Path

import psycopg
import pytest
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from agent.db import claim_inbound_batch
from agent.ownership.tests.ownership_support import BusTap, admit_owner, agent_row
from base import paths
from base.agents.history.harness_messages import aload_harness_timeline_items
from base.agents.incarnation.restart_transition import RestartTransition
from base.clock import SYSTEM_TIMERS, Clock, ClockConfig
from base.db.code_version_gate import ProcessDbGate
from base.db.transaction import async_write_transaction
from base.events.live.bus import EventBus
from base.lm.harness.registry import FileSessionRegistry, SessionRecord
from base.native_process.runtime_incarnation import RuntimeIncarnation
from services.agent_runner.agent_host.lifecycle.shells import apply_lifecycle_command
from services.agent_runner.agent_host.lifecycle.switch_note import SwitchHistory


def _record(agent_id: int, harness: str) -> SessionRecord:
    return SessionRecord(
        agent_id=agent_id,
        session_id="session-1",
        cwd="/work",
        model="m",
        created_at="2026-11-10T00:00:00+00:00",
        updated_at="2026-11-10T00:00:00+00:00",
        harness=harness,
    )


async def _claimed_self_restart(
    conn: psycopg.Connection,
    pool: AsyncConnectionPool,
    gate: ProcessDbGate,
    *,
    stored: str,
    transition: RestartTransition,
    message: str = "switching",
) -> RuntimeIncarnation:
    agent_id = agent_row(conn)
    conn.execute("UPDATE agents_meta SET harness = %s WHERE id = %s", (stored, agent_id))
    conn.commit()
    owner = await admit_owner(pool, agent_id, database_gate=gate)
    conn.execute(
        "INSERT INTO inbound_messages(agent_id,content,kind,source,payload) "
        "VALUES (%s,%s,'restart','self',%s)",
        (agent_id, message, Jsonb(transition.payload())),
    )
    conn.commit()
    [claimed] = await claim_inbound_batch(pool, agent_id, incarnation=owner)
    assert (claimed.kind, claimed.source, claimed.durable_lifecycle) == ("restart", "self", True)
    return owner


def _stored_harness(conn: psycopg.Connection, agent_id: int) -> object:
    conn.commit()
    row = conn.execute("SELECT harness FROM agents_meta WHERE id = %s", (agent_id,)).fetchone()
    assert row is not None
    return row[0]


async def test_a_switch_removes_the_old_session_record_and_is_safe_to_run_twice(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    event_bus: EventBus,
    bus_tap: BusTap,
    tmp_path: Path,
    *,
    database_gate: ProcessDbGate,
) -> None:
    owner = await _claimed_self_restart(
        db_conn,
        aops_pool,
        database_gate,
        stored="claude_code",
        transition=RestartTransition("codex", {"llm_model": "gpt-6.1-sol"}),
    )
    registry = FileSessionRegistry(paths.harness_sessions_dir(home=tmp_path))
    registry.save(_record(owner.agent_id, "claude_code"))
    await bus_tap.agent_updated_ids()

    kind = await apply_lifecycle_command(
        aops_pool, owner, bus=event_bus, resume_turn=True, home=tmp_path
    )

    assert kind == "restart"
    assert registry.load(owner.agent_id) is None
    assert _stored_harness(db_conn, owner.agent_id) == "codex"
    assert await bus_tap.agent_updated_ids() == [owner.agent_id]

    # meiva:note
    # A host that died before the commit would have its successor claim the command again and run
    # the same apply; once committed a repeat finds nothing and writes nothing.
    again = await apply_lifecycle_command(
        aops_pool, owner, bus=event_bus, resume_turn=True, home=tmp_path
    )
    assert again is None
    assert registry.load(owner.agent_id) is None
    notes = db_conn.execute(
        "SELECT count(*) FROM inbound_messages WHERE agent_id=%s AND kind='system_note'",
        (owner.agent_id,),
    ).fetchone()
    assert notes == (1,)


async def test_a_retry_after_the_record_was_already_removed_still_commits_the_switch(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    event_bus: EventBus,
    tmp_path: Path,
    *,
    database_gate: ProcessDbGate,
) -> None:
    owner = await _claimed_self_restart(
        db_conn,
        aops_pool,
        database_gate,
        stored="meiva",
        transition=RestartTransition("claude_code", {"llm_model": "claude-opus-5-5"}),
    )
    # meiva:note
    # The first attempt removed the old record and the host died before its commit: the registry
    # holds nothing, the command is still claimed, and the stored harness has not changed.
    assert _stored_harness(db_conn, owner.agent_id) == "meiva"

    kind = await apply_lifecycle_command(
        aops_pool, owner, bus=event_bus, resume_turn=True, home=tmp_path
    )

    assert kind == "restart"
    assert _stored_harness(db_conn, owner.agent_id) == "claude_code"


async def test_a_restart_that_keeps_the_harness_keeps_the_session(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    event_bus: EventBus,
    tmp_path: Path,
    *,
    database_gate: ProcessDbGate,
) -> None:
    owner = await _claimed_self_restart(
        db_conn,
        aops_pool,
        database_gate,
        stored="codex",
        transition=RestartTransition("codex", {"exec_timeout_seconds": 30}),
    )
    registry = FileSessionRegistry(paths.harness_sessions_dir(home=tmp_path))
    registry.save(_record(owner.agent_id, "codex"))

    kind = await apply_lifecycle_command(
        aops_pool, owner, bus=event_bus, resume_turn=True, home=tmp_path
    )

    assert kind == "restart"
    assert registry.load(owner.agent_id, harness="codex") == _record(owner.agent_id, "codex")
    assert _stored_harness(db_conn, owner.agent_id) == "codex"


async def test_a_malformed_transition_is_refused_before_any_effect(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    event_bus: EventBus,
    tmp_path: Path,
    *,
    database_gate: ProcessDbGate,
) -> None:
    owner = await _claimed_self_restart(
        db_conn,
        aops_pool,
        database_gate,
        stored="claude_code",
        transition=RestartTransition("codex", {}),
    )
    db_conn.execute(
        "UPDATE inbound_messages SET payload = %s WHERE agent_id = %s AND kind = 'restart'",
        (json.dumps({"restart_transition": {"target_harness": "codex"}}), owner.agent_id),
    )
    db_conn.commit()
    registry = FileSessionRegistry(paths.harness_sessions_dir(home=tmp_path))
    registry.save(_record(owner.agent_id, "claude_code"))

    with pytest.raises(TypeError, match="restart_transition"):
        _ = await apply_lifecycle_command(
            aops_pool, owner, bus=event_bus, resume_turn=True, home=tmp_path
        )

    assert registry.load(owner.agent_id) is not None
    assert _stored_harness(db_conn, owner.agent_id) == "claude_code"


async def test_leaving_an_external_harness_for_the_native_one_marks_the_outgoing_history(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    event_bus: EventBus,
    tmp_path: Path,
    *,
    database_gate: ProcessDbGate,
) -> None:
    owner = await _claimed_self_restart(
        db_conn,
        aops_pool,
        database_gate,
        stored="codex",
        transition=RestartTransition("meiva", {"llm_model": "deepseek-flash"}),
        message="back to native",
    )
    history = SwitchHistory(
        graph=None,
        pool=aops_pool,
        clock=Clock(ClockConfig("UTC", "UTC", False), timers=SYSTEM_TIMERS),
    )

    kind = await apply_lifecycle_command(
        aops_pool, owner, bus=event_bus, resume_turn=True, home=tmp_path, history=history
    )

    assert kind == "restart"
    async with async_write_transaction(aops_pool) as conn:
        [marker] = await aload_harness_timeline_items(conn, owner.agent_id)
    assert marker.kind == "system_marker"
    assert "Harness switched from 'codex' to 'meiva' by yourself: back to native" in str(
        marker.payload
    )

# meiva:note
# Restart and terminate commands: claimed at the turn boundary, executed and closed in one transaction.

from uuid import uuid4

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from agent.db import claim_inbound_batch
from agent.ownership.hosted import apply_hosted_lifecycle
from agent.ownership.tests.ownership_support import (
    admit_owner,
    agent_row,
    insert,
    insert_lifecycle_command,
)
from base.agents.inbox.insert import insert_inbound_message
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.native_process.runtime_incarnation import RuntimeIncarnation


def _status(conn: psycopg.Connection, command_id: int) -> str:
    row = conn.execute("SELECT status FROM inbound_messages WHERE id=%s", (command_id,)).fetchone()
    assert row is not None
    return row[0]


def test_request_cannot_prepopulate_reserved_result(
    db_conn: psycopg.Connection, database: Database, event_bus: EventBus
) -> None:
    agent_id = agent_row(db_conn)
    with pytest.raises(ValueError, match="reserved"):
        insert_inbound_message(
            db_conn,
            agent_id,
            "",
            "user",
            "restart",
            {"lifecycle_result": {"outcome": "superseded", "reason": "resurrect"}},
            bus=event_bus,
            database=database,
        )
    assert db_conn.execute(
        "SELECT count(*) FROM inbound_messages WHERE agent_id=%s", (agent_id,)
    ).fetchone() == (0,)


async def test_claim_takes_the_oldest_command_alone_and_leaves_the_rest_pending(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool, *, database_gate: ProcessDbGate
) -> None:
    agent_id = agent_row(db_conn)
    owner = await admit_owner(aops_pool, agent_id, database_gate=database_gate)
    first = insert_lifecycle_command(db_conn, agent_id, "restart")
    second = insert_lifecycle_command(db_conn, agent_id, "terminate")
    chat = insert(db_conn, agent_id)
    claimed = await claim_inbound_batch(aops_pool, agent_id, incarnation=owner)
    assert [(item.id, item.durable_lifecycle) for item in claimed] == [(first, True)]
    assert (_status(db_conn, first), _status(db_conn, second), _status(db_conn, chat)) == (
        "claimed",
        "pending",
        "pending",
    )
    # meiva:note
    # A second claim before the apply returns the same command, never the next one.
    again = await claim_inbound_batch(aops_pool, agent_id, incarnation=owner)
    assert [item.id for item in again] == [first]


async def test_apply_restart_releases_the_runtime_and_closes_the_command_together(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    event_bus: EventBus,
    *,
    database_gate: ProcessDbGate,
) -> None:
    agent_id = agent_row(db_conn)
    owner = await admit_owner(aops_pool, agent_id, database_gate=database_gate)
    command = insert_lifecycle_command(db_conn, agent_id, "restart")
    await claim_inbound_batch(aops_pool, agent_id, incarnation=owner)
    assert (
        await apply_hosted_lifecycle(aops_pool, owner, bus=event_bus, resume_turn=False)
        == "restart"
    )
    assert db_conn.execute(
        "SELECT status,runtime_generation,runtime_owner FROM agents_meta WHERE id=%s",
        (agent_id,),
    ).fetchone() == ("idling", None, None)
    assert db_conn.execute(
        "SELECT status,payload->'lifecycle_release' FROM inbound_messages WHERE id=%s", (command,)
    ).fetchone() == ("done", True)
    # meiva:note
    # The next admission creates a new incarnation; nothing else is owed to the command.
    successor = await admit_owner(aops_pool, agent_id, database_gate=database_gate)
    assert successor.generation != owner.generation


async def test_apply_terminate_ends_the_agent_and_closes_every_open_terminate(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    event_bus: EventBus,
    *,
    database_gate: ProcessDbGate,
) -> None:
    agent_id = agent_row(db_conn)
    owner = await admit_owner(aops_pool, agent_id, database_gate=database_gate)
    first = insert_lifecycle_command(db_conn, agent_id, "terminate")
    second = insert_lifecycle_command(db_conn, agent_id, "terminate")
    restart = insert_lifecycle_command(db_conn, agent_id, "restart")
    chat = insert(db_conn, agent_id)
    await claim_inbound_batch(aops_pool, agent_id, incarnation=owner)
    assert (
        await apply_hosted_lifecycle(aops_pool, owner, bus=event_bus, resume_turn=False)
        == "terminate"
    )
    assert db_conn.execute(
        "SELECT status,termination_source,runtime_generation FROM agents_meta WHERE id=%s",
        (agent_id,),
    ).fetchone() == ("terminated", "user", owner.generation)
    assert [_status(db_conn, i) for i in (first, second, restart, chat)] == [
        "done",
        "done",
        "pending",
        "pending",
    ]


async def test_terminate_with_a_shell_kill_request_on_any_open_terminate_kills_shells(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    event_bus: EventBus,
    *,
    database_gate: ProcessDbGate,
) -> None:
    agent_id = agent_row(db_conn)
    owner = await admit_owner(aops_pool, agent_id, database_gate=database_gate)
    insert_lifecycle_command(db_conn, agent_id, "terminate")
    insert_lifecycle_command(db_conn, agent_id, "terminate", {"kill_all_shell_sessions": True})
    db_conn.execute("UPDATE agents_meta SET session_index=7 WHERE id=%s", (agent_id,))
    db_conn.commit()
    killed: list[tuple[int, int]] = []
    await claim_inbound_batch(aops_pool, agent_id, incarnation=owner)
    kind = await apply_hosted_lifecycle(
        aops_pool,
        owner,
        bus=event_bus,
        kill_shell_sessions=lambda a, c: killed.append((a, c)),
        resume_turn=False,
    )
    assert kind == "terminate" and killed == [(agent_id, 7)]


async def test_apply_is_fenced_by_the_incarnation_and_by_a_claimed_command(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    event_bus: EventBus,
    *,
    database_gate: ProcessDbGate,
) -> None:
    agent_id = agent_row(db_conn)
    owner = await admit_owner(aops_pool, agent_id, database_gate=database_gate)
    assert await apply_hosted_lifecycle(aops_pool, owner, bus=event_bus, resume_turn=False) is None
    command = insert_lifecycle_command(db_conn, agent_id, "restart")
    await claim_inbound_batch(aops_pool, agent_id, incarnation=owner)
    stale = RuntimeIncarnation(agent_id, owner.generation, uuid4())
    assert await apply_hosted_lifecycle(aops_pool, stale, bus=event_bus, resume_turn=False) is None
    assert _status(db_conn, command) == "claimed"
    assert db_conn.execute(
        "SELECT status,runtime_owner FROM agents_meta WHERE id=%s", (agent_id,)
    ).fetchone() == ("running", owner.owner)


# meiva:note
# A terminate that asks for a shell kill without a bound killer raises mid-transaction.
async def test_a_failed_apply_leaves_neither_half_behind(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    event_bus: EventBus,
    *,
    database_gate: ProcessDbGate,
) -> None:
    agent_id = agent_row(db_conn)
    owner = await admit_owner(aops_pool, agent_id, database_gate=database_gate)
    command = insert_lifecycle_command(
        db_conn, agent_id, "terminate", {"kill_all_shell_sessions": True}
    )
    await claim_inbound_batch(aops_pool, agent_id, incarnation=owner)
    with pytest.raises(RuntimeError, match="no killer is bound"):
        await apply_hosted_lifecycle(aops_pool, owner, bus=event_bus, resume_turn=False)
    assert _status(db_conn, command) == "claimed"
    assert db_conn.execute(
        "SELECT status FROM agents_meta WHERE id=%s", (agent_id,)
    ).fetchone() == ("running",)


def _notes(
    conn: psycopg.Connection, agent_id: int
) -> list[tuple[str, str, str, dict[str, object]]]:
    return conn.execute(
        "SELECT source,status,content,payload FROM inbound_messages "
        "WHERE agent_id=%s AND kind='system_note' ORDER BY id",
        (agent_id,),
    ).fetchall()


# meiva:note
# The note that continues an unfinished turn is written in the transaction that executes the
# restart, from the restart's own source, so no restart is done without it.
@pytest.mark.parametrize(
    ("source", "who"),
    [("user", "user"), ("self", "yourself"), ("system:restart", "system:restart")],
)
async def test_apply_restart_queues_one_continuation_note_for_an_unfinished_turn(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    event_bus: EventBus,
    source: str,
    who: str,
    *,
    database_gate: ProcessDbGate,
) -> None:
    agent_id = agent_row(db_conn)
    owner = await admit_owner(aops_pool, agent_id, database_gate=database_gate)
    command = insert_lifecycle_command(db_conn, agent_id, "restart")
    db_conn.execute("UPDATE inbound_messages SET source=%s WHERE id=%s", (source, command))
    db_conn.commit()
    await claim_inbound_batch(aops_pool, agent_id, incarnation=owner)

    assert (
        await apply_hosted_lifecycle(aops_pool, owner, bus=event_bus, resume_turn=True) == "restart"
    )

    assert _status(db_conn, command) == "done"
    [(note_source, status, content, payload)] = _notes(db_conn, agent_id)
    assert (note_source, status, payload) == (source, "pending", {"note_tag": "lifecycle_restart"})
    assert content.startswith(f"Restart was accepted from {who} while your turn was unfinished.")
    assert "meiva.shell.sessions" in content


async def test_apply_restart_of_an_idle_agent_queues_no_note(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    event_bus: EventBus,
    *,
    database_gate: ProcessDbGate,
) -> None:
    agent_id = agent_row(db_conn)
    owner = await admit_owner(aops_pool, agent_id, database_gate=database_gate)
    insert_lifecycle_command(db_conn, agent_id, "restart")
    await claim_inbound_batch(aops_pool, agent_id, incarnation=owner)
    await apply_hosted_lifecycle(aops_pool, owner, bus=event_bus, resume_turn=False)
    assert _notes(db_conn, agent_id) == []


async def test_apply_terminate_queues_no_continuation_even_when_the_turn_was_unfinished(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    event_bus: EventBus,
    *,
    database_gate: ProcessDbGate,
) -> None:
    agent_id = agent_row(db_conn)
    owner = await admit_owner(aops_pool, agent_id, database_gate=database_gate)
    insert_lifecycle_command(db_conn, agent_id, "terminate")
    await claim_inbound_batch(aops_pool, agent_id, incarnation=owner)
    assert (
        await apply_hosted_lifecycle(aops_pool, owner, bus=event_bus, resume_turn=True)
        == "terminate"
    )
    assert _notes(db_conn, agent_id) == []


async def test_a_fenced_apply_queues_no_note(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    event_bus: EventBus,
    *,
    database_gate: ProcessDbGate,
) -> None:
    agent_id = agent_row(db_conn)
    owner = await admit_owner(aops_pool, agent_id, database_gate=database_gate)
    insert_lifecycle_command(db_conn, agent_id, "restart")
    await claim_inbound_batch(aops_pool, agent_id, incarnation=owner)
    stale = RuntimeIncarnation(agent_id, owner.generation, uuid4())
    assert await apply_hosted_lifecycle(aops_pool, stale, bus=event_bus, resume_turn=True) is None
    assert _notes(db_conn, agent_id) == []

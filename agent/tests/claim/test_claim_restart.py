"""The claim node's restart, restart-completed and system-note inbounds and their serial order with terminate."""

from collections.abc import Callable

import psycopg
import pytest
from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import END
from psycopg_pool import AsyncConnectionPool

from agent.graph import claim_node
from agent.graph.claim.tests.claim_support import (
    insert_inbound_kind,
    make_runtime,
    thread_config,
)
from agent.graph.claim.tests.cursor_fixture import fresh_snapshot_cursor as fresh_snapshot_cursor
from agent.state import AgentState
from agent.tests.claim.claim_status_support import committed_publishes, first_int, set_agent_status
from agent.tests.claim.claim_status_support import running_agent as running_agent
from base.agents.inbox.insert import insert_inbound_message
from base.agents.tests.langgraph_views import command_update, update_messages
from base.agents.tests.units import spawn_agent
from base.cluster.machine import MachineIdentityReader
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.config.service_read import ConfigAuthority
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.events.live.tests.live_capture import LiveEvents
from base.lm.catalog import ModelCatalog


async def test_claim_system_note_kind_appends_system_note_and_continues(
    redis_url: str,
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentityReader,
):
    """A system-note inbound retains its content and tag when claimed."""
    tid = spawn_agent(
        catalog=model_catalog,
        authority=config_authority,
        database_gate=database_gate,
        machine=machine_identity,
    )
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO inbound_messages (agent_id, content, kind, source, payload) "
            "VALUES (%s, %s, 'system_note', 'agent:405', %s::jsonb) RETURNING id",
            (
                tid,
                "Context from a peer.",
                '{"note_tag": "context"}',
            ),
        )
        inbound_id = first_int(cur.fetchone())
    db_conn.commit()

    cmd = await claim_node(
        AgentState(messages=[SystemMessage(content="sys")]),
        make_runtime(ops_pool=aops_pool, database_gate=database_gate),
        thread_config(
            tid,
        ),
    )

    assert cmd.goto == "before_turn"
    update = command_update(cmd)
    msgs = update_messages(update)
    assert len(msgs) == 1
    note = msgs[0]
    assert isinstance(note, HumanMessage)
    assert "Context from a peer." in note.content
    assert note.additional_kwargs.get("meiva_msg_type") == "system_note"
    assert note.additional_kwargs.get("meiva_note_tag") == "context"
    # The inbound row is consumed (done at claim, like other lifecycle kinds).
    with db_conn.cursor() as cur:
        cur.execute("SELECT status FROM inbound_messages WHERE id = %s", (inbound_id,))
        status = cur.fetchone()
        assert status is not None and status[0] == "done"


async def test_claim_system_note_unknown_tag_fails_loud(
    redis_url: str,
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentityReader,
):
    """A system_note inbound carrying a non-NoteTag payload fails loud — a
    writer bug must not silently render as the wrong timeline chip."""
    tid = spawn_agent(
        catalog=model_catalog,
        authority=config_authority,
        database_gate=database_gate,
        machine=machine_identity,
    )
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO inbound_messages (agent_id, content, kind, source, payload) "
            "VALUES (%s, %s, 'system_note', 'system', %s::jsonb)",
            (tid, "boom", '{"note_tag": "not_a_tag"}'),
        )
    db_conn.commit()

    with pytest.raises(ValueError, match="not a NoteTag value"):
        await claim_node(
            AgentState(messages=[SystemMessage(content="sys")]),
            make_runtime(ops_pool=aops_pool, database_gate=database_gate),
            thread_config(
                tid,
            ),
        )


async def test_claim_restart_while_idle_commits_halted_true(
    running_agent: Callable[[], int],
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    *,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentityReader,
):
    """external restart hits idle agent (halted=True) → committed halted=True,
    carrying 'no in-flight work before restart' across the restart boundary for the new process to read."""
    tid = running_agent()
    set_agent_status(db_conn, tid, "running")
    insert_inbound_kind(db_conn, tid, "", "restart", source="system:restart")

    cmd = await claim_node(
        AgentState(messages=[SystemMessage(content="sys")], halted=True),
        make_runtime(ops_pool=aops_pool, database_gate=database_gate),
        thread_config(
            tid,
        ),
    )

    assert cmd.goto == END
    assert cmd.update["halted"] is True  # type: ignore[index]


@pytest.mark.parametrize("source", ["self"])
async def test_claim_restart_self_source_commits_halted_false(
    running_agent: Callable[[], int],
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    source: str,
    *,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentityReader,
):
    """agent-initiated restart (meiva.self.restart) → even if the exec path
    set halted to True (turn-end semantics), committed halted must be False — agent has in-flight
    intent, must wake after restart to confirm result and continue."""
    tid = running_agent()
    set_agent_status(db_conn, tid, "running")
    insert_inbound_kind(db_conn, tid, "", "restart", source=source)

    cmd = await claim_node(
        AgentState(messages=[SystemMessage(content="sys")], halted=True),
        make_runtime(ops_pool=aops_pool, database_gate=database_gate),
        thread_config(
            tid,
        ),
    )

    assert cmd.goto == END
    assert cmd.update["halted"] is False  # type: ignore[index]


async def test_claim_restart_preserves_chat_for_successor(
    running_agent: Callable[[], int],
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    database: Database,
    event_bus: EventBus,
    *,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentityReader,
):
    """Only the accepted lifecycle command dispatches; chat remains durable pending work."""
    tid = running_agent()
    set_agent_status(db_conn, tid, "running")
    insert_inbound_message(db_conn, tid, "hello", source="user", bus=event_bus, database=database)
    insert_inbound_kind(db_conn, tid, "", "restart", source="system:restart")

    cmd = await claim_node(
        AgentState(messages=[SystemMessage(content="sys")], halted=True),
        make_runtime(ops_pool=aops_pool, database_gate=database_gate),
        thread_config(
            tid,
        ),
    )

    assert cmd.goto == END
    update = command_update(cmd)
    assert update["halted"] is True
    msgs = update_messages(update)
    restart_messages = msgs
    assert (
        len(restart_messages) == 1
        and "Restart was accepted" in restart_messages[0].model_dump()["content"]
    )
    assert db_conn.execute(
        "SELECT status,content FROM inbound_messages WHERE agent_id=%s AND kind='chat'", (tid,)
    ).fetchone() == ("pending", "hello")


async def test_claim_restart_kind_does_not_publish_committed(
    running_agent: Callable[[], int],
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
    live_events: LiveEvents,
    *,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentityReader,
) -> None:
    """restart kind does not publish InboundCommitted (appends no message and no chat
    inbound id enters committed list)."""
    tid = running_agent()
    set_agent_status(db_conn, tid, "running")
    insert_inbound_kind(db_conn, tid, "", "restart", source="user")

    await claim_node(
        AgentState(messages=[SystemMessage(content="sys")]),
        make_runtime(
            ops_pool=aops_pool, event_publisher=live_events.publisher, database_gate=database_gate
        ),
        thread_config(
            tid,
        ),
    )
    assert await committed_publishes(live_events) == []

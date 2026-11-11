# meiva:note
# A compact request the model cannot satisfy leaves the history, the agent and the rest of its batch intact.

from typing import cast

import httpx
import psycopg
import pytest
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage
from psycopg_pool import AsyncConnectionPool

from agent.graph import claim_node
from agent.graph.claim.tests.claim_support import insert_inbound_kind, make_runtime, thread_config
from agent.graph.hooks.compact import COMPACT_MAX_ATTEMPTS
from agent.state import AgentState
from agent.tests.claim.claim_status_support import compact_tail, pair_compact_cycles
from agent.tests.scripted_model import ScriptedChatModel
from base.agents.inbox.insert import insert_inbound_message
from base.agents.messages.kwargs import NoteTag, read_meiva_kwargs
from base.agents.tests.units import spawn_agent
from base.cluster.machine import MachineIdentityReader
from base.config.service_read import ConfigAuthority
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.events.live.tests.live_capture import LiveEvents
from base.lm.catalog import ModelCatalog


def _history(*, halted: bool = True) -> AgentState:
    messages: list[AnyMessage] = [
        SystemMessage(content="<test sys prompt>"),
        *(HumanMessage(content=f"history-{i}") for i in range(10)),
    ]
    return AgentState(messages=messages, halted=halted)


def _failure_notes(update: dict[str, object]) -> list[AnyMessage]:
    messages = cast("list[AnyMessage]", update["messages"])
    return [
        message
        for message in messages
        if read_meiva_kwargs(message).get("meiva_note_tag") == NoteTag.COMPACT_FAILED.value
    ]


# meiva:note
# A transient provider failure on every attempt: the claim retries as the auto path does, then records why the request was not applied. It raises nothing, so the host sees an ordinary turn boundary (the agent idles, no crash marker), the history is untouched, and the live run closes as a failure.
async def test_compact_request_failing_on_every_attempt_idles_with_a_note(
    redis_url: str,
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    live_events: LiveEvents,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentityReader,
):
    tid = spawn_agent(
        catalog=model_catalog,
        authority=config_authority,
        database_gate=database_gate,
        machine=machine_identity,
    )
    insert_inbound_kind(db_conn, tid, "", "compact_request")
    llm = ScriptedChatModel(replies=[httpx.ConnectError("wire lost")])

    cmd = await claim_node(
        _history(),
        make_runtime(
            ops_pool=aops_pool,
            llm=llm,
            event_publisher=live_events.publisher,
            database_gate=database_gate,
        ),
        thread_config(tid),
    )

    assert len(llm.requests) == COMPACT_MAX_ATTEMPTS
    assert cmd.update is not None
    [note] = _failure_notes(cmd.update)
    assert "ModelConnectionError: wire lost" in str(note.content)
    assert "history was not changed" in str(note.content)
    assert len(cmd.update["messages"]) == 1
    assert cmd.update["halted"] is True
    assert cmd.goto == "claim"
    [(started, finished)] = await pair_compact_cycles(live_events)
    assert started["mode"] == "request"
    assert finished["status"] == "failure"


# meiva:note
# A summary cut off by the output limit is a failed compaction, not an accepted summary: it is asked once (the same request would be cut again), the history is not replaced, and the note names the truncation.
async def test_compact_request_with_a_truncated_summary_fails_without_replacing_history(
    redis_url: str,
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentityReader,
):
    tid = spawn_agent(
        catalog=model_catalog,
        authority=config_authority,
        database_gate=database_gate,
        machine=machine_identity,
    )
    insert_inbound_kind(db_conn, tid, "", "compact_request")
    cut_off = AIMessage(content="half a summ", response_metadata={"stop_reason": "max_tokens"})
    llm = ScriptedChatModel(replies=[cut_off])

    cmd = await claim_node(
        _history(),
        make_runtime(ops_pool=aops_pool, llm=llm, database_gate=database_gate),
        thread_config(tid),
    )

    assert len(llm.requests) == 1
    assert cmd.update is not None
    [note] = _failure_notes(cmd.update)
    assert "LLMStreamTruncatedError" in str(note.content)
    assert "context_reset" not in cmd.update
    assert cmd.goto == "claim"


# meiva:note
# An exception that is not a model-call failure (a programming error) is not turned into a note: it propagates as before, and the live run is still closed as a failure.
async def test_compact_request_programming_error_propagates(
    redis_url: str,
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    live_events: LiveEvents,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentityReader,
):
    tid = spawn_agent(
        catalog=model_catalog,
        authority=config_authority,
        database_gate=database_gate,
        machine=machine_identity,
    )
    insert_inbound_kind(db_conn, tid, "", "compact_request")
    llm = ScriptedChatModel(replies=[TypeError("model code failed")])

    with pytest.raises(TypeError, match="model code failed"):
        await claim_node(
            _history(),
            make_runtime(
                ops_pool=aops_pool,
                llm=llm,
                event_publisher=live_events.publisher,
                database_gate=database_gate,
            ),
            thread_config(tid),
        )

    assert len(llm.requests) == 1
    [(_, finished)] = await pair_compact_cycles(live_events)
    assert finished["status"] == "failure"


# meiva:note
# A chat claimed in the same batch as the failing request is not dropped with it: it is delivered to the model as usual, behind the failure note.
async def test_compact_request_failure_still_delivers_the_chat_claimed_beside_it(
    redis_url: str,
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    database: Database,
    event_bus: EventBus,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentityReader,
):
    tid = spawn_agent(
        catalog=model_catalog,
        authority=config_authority,
        database_gate=database_gate,
        machine=machine_identity,
    )
    insert_inbound_kind(db_conn, tid, "", "compact_request")
    insert_inbound_message(
        db_conn, tid, "answer me anyway", source="user", bus=event_bus, database=database
    )
    llm = ScriptedChatModel(replies=[httpx.ConnectError("no answer")])

    cmd = await claim_node(
        _history(),
        make_runtime(ops_pool=aops_pool, llm=llm, database_gate=database_gate),
        thread_config(tid),
    )

    assert cmd.update is not None
    messages = cmd.update["messages"]
    assert len(_failure_notes(cmd.update)) == 1
    assert any("answer me anyway" in str(message.content) for message in messages)
    assert cmd.goto == "before_turn"
    assert cmd.update["halted"] is False


# meiva:note
# A request that fails while the agent is in the middle of a turn must not stop that turn: the agent goes on to read the note and decide. An idle agent, as in the first test, stays idle.
async def test_compact_request_failure_in_a_running_turn_wakes_the_agent_to_read_it(
    redis_url: str,
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentityReader,
):
    tid = spawn_agent(
        catalog=model_catalog,
        authority=config_authority,
        database_gate=database_gate,
        machine=machine_identity,
    )
    insert_inbound_kind(db_conn, tid, "", "compact_request")
    llm = ScriptedChatModel(replies=[httpx.ConnectError("wire lost")])

    cmd = await claim_node(
        _history(halted=False),
        make_runtime(ops_pool=aops_pool, llm=llm, database_gate=database_gate),
        thread_config(tid),
    )

    assert cmd.update is not None
    assert len(_failure_notes(cmd.update)) == 1
    assert cmd.goto == "before_turn"
    assert cmd.update["halted"] is False


# meiva:note
# A request that fails supersedes nothing: when an earlier request of the same batch produced a summary, that summary is still applied, its live run ends once as a success, and only the failed run ends as a failure. The failure note rides behind the summary.
async def test_compact_request_failing_after_a_successful_one_keeps_the_earlier_summary(
    redis_url: str,
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    live_events: LiveEvents,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentityReader,
):
    tid = spawn_agent(
        catalog=model_catalog,
        authority=config_authority,
        database_gate=database_gate,
        machine=machine_identity,
    )
    insert_inbound_kind(db_conn, tid, "", "compact_request")
    insert_inbound_kind(db_conn, tid, "", "compact_request")
    llm = ScriptedChatModel(
        replies=[AIMessage(content="first summary"), httpx.ConnectError("wire lost")]
    )

    cmd = await claim_node(
        _history(),
        make_runtime(
            ops_pool=aops_pool,
            llm=llm,
            event_publisher=live_events.publisher,
            database_gate=database_gate,
        ),
        thread_config(tid),
    )

    assert cmd.goto == "init_context"
    tail = compact_tail(cmd.update)
    assert "first summary" in str(tail[0].content)
    statuses = sorted(finished["status"] for _, finished in await pair_compact_cycles(live_events))
    assert statuses == ["failure", "success"]

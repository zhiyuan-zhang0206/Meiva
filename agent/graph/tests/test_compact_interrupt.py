from __future__ import annotations

import asyncio
import random
from typing import Any, cast

import psycopg
import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.graph.message import add_messages
from langgraph.runtime import Runtime
from langgraph.types import Command
from psycopg_pool import AsyncConnectionPool

from agent.graph._init_context import init_context_node
from agent.graph.claim.node import claim_node
from agent.graph.hooks.compact import auto_compact_will_fire, compact_reminder
from agent.graph.hooks.tests.scripted_model import HangingSummarizer, ScriptedModel
from agent.graph.interrupt.tests.queued_commands import queue_command
from agent.graph.llm.node import llm_node
from agent.graph.llm_errors import LlmLedger
from agent.state import AgentState, CompactState, checkpoint_msgpack_allowlist
from base.agents.context import MeivaContext
from base.agents.tests.units import spawn_agent
from base.clock import Clock
from base.clock.tests.seeded_rng import seeded_rng
from base.cluster.machine import MachineIdentityReader
from base.config.service_read import ConfigAuthority
from base.config.tests.configuration import defaults
from base.db import Database, create_agent
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.events.live.tests.live_capture import LiveEvents
from base.host.env.agent_slices import AgentSlices
from base.lm.catalog import ModelCatalog
from base.native_process.stop_timing import RESTART_INTERRUPT_GRACE_S
from base.native_process.turn_identity import HostedTurnResources
from base.packages.composer_commands import CommandCatalog
from base.packages.plugin_providers import build_model_catalog
from base.paths import resolve_meiva_home
from meiva.tests.fixtures.pin_agent import hosted_resources as hosted_resources


# meiva:note
# A fresh seeded generator for each use, so no test sees another's draws.
def _rng() -> random.Random:
    return random.Random(0)  # noqa: S311


# meiva:note
# The watcher polls on its production cadence here, so a cancel queued while the summary runs is
# noticed within one poll interval; these windows leave generous room for it.
_INTERRUPT_WINDOW_S = 15.0


def _runtime(
    model: object,
    *,
    hosted_resources: HostedTurnResources,
    ops_pool: AsyncConnectionPool,
    event_publisher: object,
    database_gate: ProcessDbGate,
) -> Runtime[MeivaContext]:
    return Runtime(
        context=MeivaContext(
            meiva_home=resolve_meiva_home(),
            hosted_resources=hosted_resources,
            ops_pool=ops_pool,
            llm=cast(BaseChatModel, model),
            event_publisher=cast(Any, event_publisher),
            agent=AgentSlices.resolve(
                {"auto_compact_fraction": 1e-6, "compact_reminder_fraction": 1e-6},
                default_reader=defaults(),
            ),
            db=Database.from_settings(gate=database_gate),
            bus=EventBus.from_settings(rng=seeded_rng()),
            catalog=build_model_catalog(),
            clock_factory=Clock.from_settings,
            commands=CommandCatalog.system(skills=list),
        )
    )


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        ("cancel", {"halted": True}),
        ("restart", {"halted": True, "interrupted_by_restart": True}),
    ],
)
async def test_auto_compaction_cancels_at_llm_node_without_replacing_context(
    hosted_resources: HostedTurnResources,
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    live_events: LiveEvents,
    database_gate: ProcessDbGate,
    kind: str,
    expected: dict[str, bool],
) -> None:
    tid = create_agent(db_conn)
    model = HangingSummarizer()
    state = AgentState(
        messages=[HumanMessage(content="uncompacted original work " * 30)],
        compact=CompactState(version=4),
        halted=False,
    )
    runtime = _runtime(
        model,
        hosted_resources=hosted_resources,
        ops_pool=aops_pool,
        event_publisher=live_events.publisher,
        database_gate=database_gate,
    )
    invocation = asyncio.create_task(
        llm_node(
            state,
            runtime,
            {"configurable": {"thread_id": str(tid)}},
            ledger=LlmLedger(),
            rng=_rng(),
        )
    )
    try:
        await asyncio.wait_for(model.started.wait(), timeout=_INTERRUPT_WINDOW_S)
        queue_command(db_conn, tid, kind, waited_s=RESTART_INTERRUPT_GRACE_S + 2)
        result = await asyncio.wait_for(invocation, timeout=_INTERRUPT_WINDOW_S)
    finally:
        invocation.cancel()
        await asyncio.gather(invocation, return_exceptions=True)
    assert model.settled.is_set()
    assert result.goto == "claim"
    assert result.update == expected
    assert state.compact.version == 4
    assert state.context_reset.tail == []
    assert state.messages[0].content == "uncompacted original work " * 30
    assert db_conn.execute(
        "SELECT status,claimed_at FROM inbound_messages WHERE agent_id=%s",
        (tid,),
    ).fetchall() == [("pending", None)]
    compact_events = [
        event for event in await live_events.events() if event["role"].startswith("compact_")
    ]
    assert [event["role"] for event in compact_events] == ["compact_started", "compact_finished"]
    assert compact_events[1]["status"] == "replaced"
    assert compact_events[0]["compact_id"] == compact_events[1]["compact_id"]


def _apply(state: AgentState, command: Command[Any]) -> AgentState:
    # meiva:note
    # Applies a node's update the way the graph's reducers do: messages append through
    # `add_messages`, every other key replaces. `Command.update` is typed loosely upstream.
    assert isinstance(command.update, dict)
    update = dict(cast("dict[str, Any]", command.update))  # pyright: ignore[reportUnknownMemberType]
    if "messages" in update:
        update["messages"] = add_messages(list(state.messages), update["messages"])
    return state.model_copy(update=update)


@pytest.mark.parametrize("marker", ["compact_summary", "compact_request"])
async def test_compaction_returns_through_claim_then_generates_before_compacting_again(
    redis_url: str,
    hosted_resources: HostedTurnResources,
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    live_events: LiveEvents,
    marker: str,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentityReader,
) -> None:
    model = ScriptedModel(
        AIMessage(content="the retained summary is still above the ceiling " * 30),
        generation=AIMessageChunk(
            content="resumed ordinary work",
            response_metadata={"model_provider": "anthropic", "stop_reason": "end_turn"},
            usage_metadata={"input_tokens": 500, "output_tokens": 5, "total_tokens": 505},
        ),
    )
    tid = spawn_agent(
        catalog=model_catalog,
        authority=config_authority,
        database_gate=database_gate,
        machine=machine_identity,
    )
    config: RunnableConfig = {"configurable": {"thread_id": str(tid)}}
    runtime = _runtime(
        model,
        hosted_resources=hosted_resources,
        ops_pool=aops_pool,
        event_publisher=live_events.publisher,
        database_gate=database_gate,
    )
    state = AgentState(messages=[HumanMessage(content="old work " * 100)], halted=False)
    compacted = await llm_node(state, runtime, config, ledger=LlmLedger(), rng=_rng())
    assert compacted.goto == "init_context"
    state = _apply(state, compacted)
    state.context_reset.tail[0].additional_kwargs["meiva_msg_type"] = marker
    rebuilt = await init_context_node(state, runtime, config)
    assert rebuilt.goto == "claim"
    state = _apply(state, rebuilt)

    # meiva:note
    # A real checkpoint round trip and a cancel must preserve the exemption.
    serde = JsonPlusSerializer(allowed_msgpack_modules=checkpoint_msgpack_allowlist())
    state = state.model_copy(
        update={"messages": serde.loads_typed(serde.dumps_typed(state.messages))}
    )
    catalog = build_model_catalog()
    assert not auto_compact_will_fire(state, runtime.context.require_agent(), catalog=catalog)
    queue_command(db_conn, tid, "cancel")
    cancelled = await claim_node(state, runtime, config)
    assert cancelled.goto == "claim"
    state = _apply(state, cancelled)
    assert state.halted
    assert not auto_compact_will_fire(state, runtime.context.require_agent(), catalog=catalog)

    db_conn.execute(
        "INSERT INTO inbound_messages(agent_id,kind,source,content) VALUES(%s,'chat','user','continue')",
        (tid,),
    )
    db_conn.commit()
    resumed = await claim_node(state, runtime, config)
    assert resumed.goto == "before_llm"
    state = _apply(state, resumed)
    assert not auto_compact_will_fire(state, runtime.context.require_agent(), catalog=catalog)
    assert await compact_reminder(state, runtime, config) is None
    generated = await llm_node(state, runtime, config, ledger=LlmLedger(), rng=_rng())
    assert generated.goto == "after_exec"
    state = _apply(state, generated)
    assert state.messages[-1].content == "resumed ordinary work"
    assert state.compact.version == 1
    # meiva:note
    # A committed ordinary result re-arms the threshold.
    assert auto_compact_will_fire(state, runtime.context.require_agent(), catalog=catalog)

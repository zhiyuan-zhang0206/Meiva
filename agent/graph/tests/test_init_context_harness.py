# meiva:link agent/graph/docs/harness-turn.meiva.okf.md

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import psycopg
import pytest
from langchain_core.messages import AnyMessage, HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langgraph.runtime import Runtime
from psycopg_pool import AsyncConnectionPool

from agent.graph._init_context import init_context_node
from agent.messages import NoteTag, system_note_message
from agent.state import AgentState, CapabilitiesState, ContextReset
from base.agents.context import MeivaContext
from base.agents.context.identity import AgentIdentity
from base.agents.history.harness_messages import load_harness_timeline_items
from base.api_contracts.agent_contract import Harness
from base.clock import SYSTEM_TIMERS, Clock
from base.clock.tests.seeded_rng import seeded_rng
from base.config.tests.configuration import defaults
from base.db import Database, create_agent
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.host.env.agent_slices import AgentSlices
from base.lm.catalog import ModelCatalog
from base.packages.plugin_providers import build_model_catalog
from base.packages.plugins.extensions import (
    EMPTY,
    ContextNote,
    ExtensionRegistry,
    PluginContributions,
)
from base.paths import meiva_home, resolve_meiva_home
from meiva.tests.fixtures.pin_agent import snapshot_clients

_PICKED_UP_AT = datetime(2026, 1, 1, tzinfo=UTC)


def _config(tid: int) -> RunnableConfig:
    return {"configurable": {"thread_id": str(tid)}}


def _runtime(
    ops_pool: AsyncConnectionPool[Any] | None,
    extensions: ExtensionRegistry = EMPTY,
    *,
    harness: Harness = Harness.MEIVA,
    harness_bridge: Any = None,
    agent_id: int | None = None,
    database_gate: ProcessDbGate,
) -> Runtime[MeivaContext]:
    return Runtime(
        context=MeivaContext(
            meiva_home=resolve_meiva_home(),
            environ=os.environ,
            user_home=Path.home(),
            identity=AgentIdentity(agent_id=agent_id, owns_loop=True),
            clients=snapshot_clients(),
            harness=harness,
            harness_bridge=harness_bridge,
            ops_pool=ops_pool,
            llm=AsyncMock(),
            event_publisher=MagicMock(),
            agent=AgentSlices.resolve(None, default_reader=defaults()),
            extensions=extensions,
            db=Database.from_settings(gate=database_gate),
            bus=EventBus.from_settings(rng=seeded_rng(), timers=SYSTEM_TIMERS),
            catalog=build_model_catalog(home=meiva_home()),
            clock_factory=Clock.from_settings,
        )
    )


@pytest.mark.parametrize("harness", [Harness.CLAUDE_CODE, Harness.CODEX, Harness.ANTIGRAVITY])
async def test_external_harness_cold_start_establishes_handoff_and_standing_notes(
    harness: Harness,
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
    database_gate: ProcessDbGate,
) -> None:
    tid = create_agent(db_conn)

    def custom_section(_slices: AgentSlices, *, catalog: ModelCatalog) -> str:
        del catalog
        return "# Custom Plugin Section\n\nFollow the cluster workflow."

    def custom_note(_ctx: MeivaContext) -> HumanMessage:
        return system_note_message(
            content="Custom plugin standing note.", tag=NoteTag.MEMORY, picked_up_at=_PICKED_UP_AT
        )

    registry = ExtensionRegistry(
        (
            (
                "demo",
                PluginContributions(
                    system_prompt_sections=(custom_section,),
                    context_notes=(ContextNote(custom_note),),
                ),
            ),
        )
    )

    handoffs: list[object] = []
    bridge = SimpleNamespace(session_id=None, set_handoff=handoffs.append)
    runtime = _runtime(
        aops_pool,
        registry,
        harness=harness,
        harness_bridge=bridge,
        agent_id=tid,
        database_gate=database_gate,
    )
    command = await init_context_node(AgentState(), runtime, _config(tid))

    assert command.update is not None and "messages" not in command.update
    items = load_harness_timeline_items(db_conn, tid)
    assert len(items) >= 3 and items[0].kind == "system_prompt"
    head_text = str(items[0].payload)
    assert all(
        s in head_text
        for s in (
            f"# Role & Operating Context — Agent #{tid}",
            "meiva.self",
            "pause_heartbeat",
            "# Custom Plugin Section",
        )
    )
    note_texts = "\n".join(str(it.payload) for it in items[1:])
    assert all(
        s in note_texts
        for s in (
            "Conversation timezone:",
            f"Your Agent ID is {tid}",
            "Custom plugin standing note.",
        )
    )
    assert (
        isinstance(command.update["capabilities"], CapabilitiesState)
        and command.update["context_reset"] == ContextReset()
        and command.goto == "claim"
        and len(handoffs) == 1
    )
    bridge.session_id = "warm-session-1"
    _ = await init_context_node(AgentState(), runtime, _config(tid))
    assert len(handoffs) == 1 and len(load_harness_timeline_items(db_conn, tid)) == len(items)


@pytest.mark.parametrize("harness", [Harness.CLAUDE_CODE, Harness.CODEX])
async def test_external_context_reset_preserves_the_tail_behind_handoff(
    harness: Harness,
    database_gate: ProcessDbGate,
) -> None:
    tail: list[AnyMessage] = [
        HumanMessage(content="The real compact summary", id="compact-summary")
    ]
    command = await init_context_node(
        AgentState(context_reset=ContextReset(tail=tail, resume="before_llm")),
        _runtime(None, harness=harness, database_gate=database_gate),
        _config(42),
    )

    assert command.update is not None
    assert "messages" not in command.update
    prompt = command.update["harness_pending_input"]["prompt"]
    assert "# Role & Operating Context — Agent #42" in prompt
    assert prompt.endswith("The real compact summary")
    assert command.update["context_reset"] == ContextReset()
    assert command.goto == "harness_turn"


@pytest.mark.parametrize("harness", [Harness.CLAUDE_CODE, Harness.CODEX])
async def test_existing_external_history_and_cursor_are_not_rewritten(
    harness: Harness,
    database_gate: ProcessDbGate,
) -> None:
    state = AgentState(
        messages=[SystemMessage(content="Legacy local prompt"), HumanMessage(content="Task")],
    )
    command = await init_context_node(
        state,
        _runtime(None, harness=harness, database_gate=database_gate),
        _config(42),
    )

    assert command.update is not None
    assert "messages" not in command.update
    assert state.messages[0].content == "Legacy local prompt"


async def test_the_built_in_loop_writes_no_cursor(
    database_gate: ProcessDbGate,
) -> None:
    command = await init_context_node(
        AgentState(), _runtime(None, database_gate=database_gate), _config(42)
    )

    assert command.update is not None
    assert "messages" in command.update
    assert "harness_pending_input" not in command.update

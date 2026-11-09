# meiva:link agent/graph/docs/harness-turn.md

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import replace
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
from agent.graph.claim.harness_turn import harness_turn_node
from agent.graph.claim.node import claim_node
from agent.graph.hooks.framework import hooks_at
from agent.graph.hooks.node import hook_node
from agent.messages import NoteTag, system_note_message
from agent.state import AgentState, CapabilitiesState, ContextReset, SkillRecallState
from base import paths
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
from base.lm.harness import Usage
from base.lm.harness.engine import HarnessTurn
from base.lm.provider_api import Decision, DecisionResult, Question
from base.packages.composer_commands import CommandCatalog
from base.packages.extensions import install_registry
from base.packages.extensions.install_registry import InstalledPackage
from base.packages.plugin_providers import build_model_catalog
from base.packages.plugins.extensions import (
    EMPTY,
    ContextNote,
    ExtensionRegistry,
    PluginContributions,
)
from base.packages.tests.model_catalog import inert_keys
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
        AgentState(context_reset=ContextReset(tail=tail, resume="before_turn")),
        _runtime(None, harness=harness, database_gate=database_gate),
        _config(42),
    )

    assert command.update is not None
    assert "messages" not in command.update
    prompt = command.update["harness_pending_input"]["prompt"]
    assert "# Role & Operating Context — Agent #42" in prompt
    assert prompt.endswith("The real compact summary")
    assert command.update["context_reset"] == ContextReset()
    assert command.goto == "before_turn"


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


def _make_antigravity_recall_runtime(
    *,
    tid: int,
    unit_home: Path,
    aops_pool: AsyncConnectionPool[Any],
    event_bus: EventBus,
    database_gate: ProcessDbGate,
    scorer_calls: list[str],
    recorded_prompts: list[str],
) -> Runtime[MeivaContext]:
    class _Scorer:
        async def decide(self, state: str, questions: Mapping[str, Question]) -> DecisionResult:
            scorer_calls.append(state)
            return DecisionResult(
                {
                    qid: Decision(
                        top="true" if qid == "beta" else "false",
                        probabilities={"true": 0.9, "false": 0.1}
                        if qid == "beta"
                        else {"true": 0.0, "false": 1.0},
                    )
                    for qid in questions
                },
                input_tokens=50,
            )

    base_catalog = build_model_catalog(home=unit_home)
    provider = base_catalog.decision_provider_of("jev-1.13.0")
    assert provider is not None

    def _build_scorer(_ctx: Any) -> _Scorer:
        return _Scorer()

    catalog = replace(
        base_catalog,
        decision_providers={
            **base_catalog.decision_providers,
            provider.prefix: replace(provider, build=_build_scorer),
        },
    )

    class _Bridge:
        model = "claude-sonnet-5"

        async def run_turn(self, prompt: str, *, key: str, on_event: Any = None) -> HarnessTurn:
            del key, on_event
            recorded_prompts.append(prompt)
            return HarnessTurn(
                turn_id="t1",
                text="MEIVA_DISPLAY_TO_USER\nRelease notes drafted.",
                usage=Usage(0, 0),
                cost_usd=None,
                subtype="success",
                is_error=False,
                interrupted=False,
            )

    bridge: Any = _Bridge()
    return Runtime(
        context=MeivaContext(
            meiva_home=unit_home,
            environ=os.environ,
            user_home=Path.home(),
            identity=AgentIdentity(agent_id=tid, owns_loop=True),
            clients=snapshot_clients(),
            harness=Harness.ANTIGRAVITY,
            harness_bridge=bridge,
            ops_pool=aops_pool,
            llm=AsyncMock(),
            event_publisher=MagicMock(),
            agent=AgentSlices.resolve(
                {
                    "skills_to_inject_into_system_prompt": ["*"],
                    "skills_to_expand_at_start": [],
                },
                default_reader=defaults(),
            ),
            extensions=EMPTY,
            db=Database.from_settings(gate=database_gate),
            bus=event_bus,
            catalog=catalog,
            provider_keys=inert_keys(catalog),
            clock_factory=Clock.from_settings,
            commands=CommandCatalog.system(skills=list, home=unit_home),
        )
    )


def _seed_beta_skill_and_inbound(
    unit_home: Path, db_conn: psycopg.Connection[Any], tid: int
) -> None:
    skills = paths.skills_dir(unit_home)
    (skills / "beta").mkdir(parents=True, exist_ok=True)
    (skills / "beta" / "SKILL.md").write_text(
        "---\nname: beta\ndescription: beta release skill\n---\n\nBODY\n",
        encoding="utf-8",
    )
    install_registry.register(InstalledPackage(name="beta", type="skill"), home=unit_home)
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agents_meta (id, status) VALUES (%s, 'running') "
            "ON CONFLICT (id) DO UPDATE SET status = 'running'",
            (tid,),
        )
        cur.execute(
            "INSERT INTO inbound_messages (agent_id, content, source, kind) "
            "VALUES (%s, %s, 'user', 'chat')",
            (tid, "please use beta to draft the release notes"),
        )
    db_conn.commit()


async def test_antigravity_claim_before_turn_harness_turn_recalls_skill_and_prepends_prompt(
    unit_home: Path,
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
    event_bus: EventBus,
    database_gate: ProcessDbGate,
) -> None:
    tid = create_agent(db_conn)
    _seed_beta_skill_and_inbound(unit_home, db_conn, tid)

    scorer_calls: list[str] = []
    recorded_prompts: list[str] = []
    runtime = _make_antigravity_recall_runtime(
        tid=tid,
        unit_home=unit_home,
        aops_pool=aops_pool,
        event_bus=event_bus,
        database_gate=database_gate,
        scorer_calls=scorer_calls,
        recorded_prompts=recorded_prompts,
    )
    cfg = _config(tid)

    claim_cmd = await claim_node(AgentState(), runtime, cfg)
    assert claim_cmd.goto == "before_turn"
    assert claim_cmd.update is not None
    pending: dict[str, Any] = claim_cmd.update["harness_pending_input"]
    assert len(pending["messages"]) == 1

    state_after_claim = AgentState(**claim_cmd.update)
    before_turn_fn = hook_node(
        "before_turn", lambda _s: "harness_turn", hooks_at("before_turn", EMPTY)
    )
    bt_cmd = await before_turn_fn(state_after_claim, runtime, cfg)
    assert bt_cmd.goto == "harness_turn"
    assert bt_cmd.update is not None
    assert "please use beta to draft the release notes" in scorer_calls[0]
    assert bt_cmd.update["skill_recall"] == SkillRecallState(prompted={"beta"}, window=0)
    updated_pending: dict[str, Any] = bt_cmd.update["harness_pending_input"]
    updated_prompt: str = updated_pending["prompt"]
    assert updated_prompt.index("meiva.skills.beta") < updated_prompt.index(
        "please use beta to draft the release notes"
    )

    state_after_bt = state_after_claim.model_copy(update=bt_cmd.update)
    assert state_after_bt.messages == []
    ht_cmd = await harness_turn_node(
        state_after_bt,
        Runtime(context=replace(runtime.context, ops_pool=None)),
        cfg,
    )
    assert ht_cmd.goto == "claim"
    assert "meiva.skills.beta" in recorded_prompts[0]
    assert "please use beta to draft the release notes" in recorded_prompts[0]

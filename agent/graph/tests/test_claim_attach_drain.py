from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock

import psycopg
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.runtime import Runtime
from psycopg_pool import AsyncConnectionPool

from agent.graph._attach_drain import build_attach_drain
from agent.graph.claim.node import claim_node_impl
from agent.nodes import CLAIM
from agent.state import AttachEntry, AttachState, BaseAgentState
from base.agents.context import MeivaContext
from base.agents.messages.kwargs import MeivaMsgType
from base.agents.tests.units import spawn_agent
from base.clock import Clock
from base.cluster.machine import MachineIdentity
from base.config.service_read import ConfigAuthority
from base.config.tests.configuration import defaults
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.host.env.agent_slices import AgentSlices
from base.lm.catalog import ModelCatalog
from base.packages.plugin_providers import build_model_catalog


def _write_png(path: Path) -> None:
    from PIL import Image

    Image.new("RGB", (1, 1)).save(path)


def _context(
    model_name: str, database_gate: ProcessDbGate, ops_pool: AsyncConnectionPool | None = None
) -> MeivaContext:
    return MeivaContext(
        ops_pool=ops_pool,
        llm=cast("BaseChatModel", SimpleNamespace(model_name=model_name)),
        event_publisher=MagicMock(),
        agent=AgentSlices.resolve(default_reader=defaults()),
        db=Database.from_settings(gate=database_gate),
        bus=EventBus.from_settings(),
        catalog=build_model_catalog(),
        clock_factory=Clock.from_settings,
    )


def _blocks(message: HumanMessage) -> list[dict[str, Any]]:
    content = cast(Any, message).content
    assert isinstance(content, list)
    return cast("list[dict[str, Any]]", content)


def test_drain_builds_one_native_image_message(
    tmp_path: Path, database_gate: ProcessDbGate
) -> None:
    image = tmp_path / "render.png"
    _write_png(image)
    state = BaseAgentState(
        attach=AttachState(pending=[AttachEntry(path=str(image.resolve()), label="after fix")])
    )

    drain = build_attach_drain(state, _context("glm-5.3-flash", database_gate=database_gate))

    assert drain is not None
    assert drain["attach"] == AttachState()
    message = drain["messages"][0]
    assert isinstance(message, HumanMessage)
    assert cast(Any, message).additional_kwargs["meiva_msg_type"] == MeivaMsgType.ATTACH.value
    blocks = _blocks(message)
    assert [b["type"] for b in blocks] == ["text", "text", "image_url"]
    assert "Files attached during this turn" in blocks[0]["text"]
    assert "after fix" in blocks[1]["text"]
    assert blocks[2]["type"] == "image_url"


def test_drain_keeps_text_only_models_informed(
    tmp_path: Path, database_gate: ProcessDbGate
) -> None:
    image = tmp_path / "render.png"
    _write_png(image)
    state = BaseAgentState(attach=AttachState(pending=[AttachEntry(path=str(image), label=None)]))

    drain = build_attach_drain(state, _context("glm-5.3", database_gate=database_gate))

    assert drain is not None
    message = drain["messages"][0]
    assert isinstance(message, HumanMessage)
    blocks = _blocks(message)
    assert [b["type"] for b in blocks] == ["text", "text"]
    assert "your model cannot receive image" in blocks[1]["text"]


def test_drain_is_noop_without_pending_attachments(database_gate: ProcessDbGate) -> None:
    assert (
        build_attach_drain(
            BaseAgentState(), _context("deepseek-flash", database_gate=database_gate)
        )
        is None
    )


async def test_claim_drains_before_turn_boundary_wait(
    redis_url: str,
    tmp_path: Path,
    database_gate: ProcessDbGate,
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    machine_identity: MachineIdentity,
) -> None:
    image = tmp_path / "render.png"
    _write_png(image)
    state = BaseAgentState(
        halted=True,
        attach=AttachState(pending=[AttachEntry(path=str(image), label=None)]),
    )
    runtime = Runtime(
        context=_context("glm-5.3-flash", database_gate=database_gate, ops_pool=aops_pool)
    )
    agent_id = spawn_agent(
        catalog=model_catalog,
        authority=config_authority,
        database_gate=database_gate,
        machine=machine_identity,
    )
    config: RunnableConfig = {"configurable": {"thread_id": str(agent_id)}}

    command = await claim_node_impl(state, runtime, config)

    assert command.goto == CLAIM
    assert command.update is not None
    assert cast("dict[str, Any]", command.update)["attach"] == AttachState()


async def test_claim_does_not_drain_mid_turn(
    redis_url: str,
    tmp_path: Path,
    database_gate: ProcessDbGate,
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    machine_identity: MachineIdentity,
) -> None:
    image = tmp_path / "render.png"
    _write_png(image)
    state = BaseAgentState(
        messages=[HumanMessage(content="continue working")],
        attach=AttachState(pending=[AttachEntry(path=str(image), label=None)]),
    )
    runtime = Runtime(
        context=_context("glm-5.3-flash", database_gate=database_gate, ops_pool=aops_pool)
    )
    agent_id = spawn_agent(
        catalog=model_catalog,
        authority=config_authority,
        database_gate=database_gate,
        machine=machine_identity,
    )
    config: RunnableConfig = {"configurable": {"thread_id": str(agent_id)}}

    command = await claim_node_impl(state, runtime, config)

    assert command.goto == "before_llm"
    assert command.update is not None
    assert "attach" not in cast("dict[str, Any]", command.update)

# pyright: reportUnknownMemberType = warning
# pyright: reportUnknownArgumentType = warning
# pyright: reportUnknownLambdaType = warning
# pyright: reportUnknownVariableType = warning
"""The llm node carries the understanding cut on whichever command ends the turn."""

import random
from collections.abc import AsyncIterator
from unittest.mock import MagicMock

import psycopg
import pytest
from langchain_core.messages import AIMessageChunk, AnyMessage, HumanMessage, SystemMessage
from langchain_core.messages.ai import UsageMetadata
from langgraph.runtime import Runtime
from langgraph.types import Command
from psycopg_pool import AsyncConnectionPool

from agent.graph import LlmLedger, llm_node
from agent.state import AgentState
from agent.state_channels import CompactState
from base.agents.context import MeivaContext
from base.agents.context.identity import AgentIdentity
from base.clock import SYSTEM_TIMERS, Clock
from base.clock.tests.seeded_rng import seeded_rng
from base.config.tests.configuration import defaults
from base.db import Database, create_agent
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.host.env.agent_slices import AgentSlices
from base.native_process.turn_identity import HostedTurnResources
from base.packages.plugin_providers import build_model_catalog
from base.paths import meiva_home
from meiva.tests.fixtures.pin_agent import hosted_resources as hosted_resources


# meiva:note
# A fresh seeded generator for each use, so no test sees another's draws.
def _rng() -> random.Random:
    return random.Random(0)  # noqa: S311


async def _aiter(chunks: list[AIMessageChunk]) -> AsyncIterator[AIMessageChunk]:
    for c in chunks:
        yield c


def _runtime(
    chunks: list[AIMessageChunk],
    database_gate: ProcessDbGate,
    ops_pool: AsyncConnectionPool,
    agent_id: int,
    hosted_resources: HostedTurnResources,
) -> Runtime[MeivaContext]:
    llm = MagicMock()
    llm.bind_tools.return_value = llm
    llm.astream.return_value = _aiter(chunks)
    return Runtime(
        context=MeivaContext(
            hosted_resources=hosted_resources,
            ops_pool=ops_pool,
            llm=llm,
            event_publisher=MagicMock(),
            agent=AgentSlices.resolve(
                {
                    "llm_model": _MODEL,
                    "understanding_enabled": True,
                    "understanding_chunk_ratio": _RATIO,
                },
                default_reader=defaults(),
            ),
            db=Database.from_settings(gate=database_gate),
            bus=EventBus.from_settings(rng=seeded_rng(), timers=SYSTEM_TIMERS),
            identity=AgentIdentity(agent_id=agent_id, owns_loop=True),
            catalog=build_model_catalog(home=meiva_home()),
            clock_factory=Clock.from_settings,
        )
    )


_MODEL = "deepseek-flash"
# meiva:note
# A real model and a small ratio: the chunk size is about 750 tokens, between the 2000 tokens the
# first turn grows by and the shrinkage of the second.
_RATIO = 0.002


def _state() -> AgentState:
    messages: list[AnyMessage] = [
        SystemMessage(content="head", id="h"),
        HumanMessage(content="go", id="m1"),
    ]
    # Past the segment's first turn: the cut sits after the head with a 500-token baseline.
    compact = CompactState(version=3, understanding_cut_index=1, understanding_cut_tokens=500)
    return AgentState(messages=messages, halted=False, compact=compact)


def _turn(input_tokens: int, *, tool: bool) -> list[AIMessageChunk]:
    usage = UsageMetadata(input_tokens=input_tokens, output_tokens=1, total_tokens=input_tokens + 1)
    if tool:
        return [
            AIMessageChunk(
                content="ok",
                tool_call_chunks=[
                    {"name": "execute_code", "args": '{"code": "1"}', "id": "c1", "index": 0}
                ],
                response_metadata={"model_provider": "anthropic", "stop_reason": "tool_use"},
                usage_metadata=usage,
            )
        ]
    return [
        AIMessageChunk(
            content="done",
            response_metadata={"model_provider": "anthropic", "stop_reason": "end_turn"},
            usage_metadata=usage,
        )
    ]


async def _jobs(pool: AsyncConnectionPool) -> list[tuple[int, int, int, int, str]]:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT agent_id, compact_version, start_index, end_index, end_msg_id"
            " FROM understanding_chunk_jobs ORDER BY id"
        )
        return await cur.fetchall()


@pytest.mark.parametrize("tool", [True, False])
async def test_cut_rides_the_tool_and_the_idle_command(
    redis_url: str,
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    hosted_resources: HostedTurnResources,
    tool: bool,
    database_gate: ProcessDbGate,
) -> None:
    agent_id = create_agent(db_conn)
    result = await llm_node(
        _state(),
        _runtime(_turn(2500, tool=tool), database_gate, aops_pool, agent_id, hosted_resources),
        {"configurable": {"thread_id": str(agent_id)}},
        ledger=LlmLedger(),
        rng=_rng(),
    )
    assert isinstance(result, Command)
    assert result.update["compact"].understanding_cut_index == 2  # type: ignore[index]
    assert result.update["compact"].understanding_cut_tokens == 2500  # type: ignore[index]
    assert result.update["compact"].version == 3  # type: ignore[index]
    # meiva:note
    # The request's last message, not the reply, ends the stretch.
    assert await _jobs(aops_pool) == [(agent_id, 3, 1, 2, "m1")]


async def test_turn_under_the_threshold_leaves_the_state_alone(
    redis_url: str,
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    hosted_resources: HostedTurnResources,
    database_gate: ProcessDbGate,
) -> None:
    agent_id = create_agent(db_conn)
    result = await llm_node(
        _state(),
        _runtime(_turn(10, tool=True), database_gate, aops_pool, agent_id, hosted_resources),
        {"configurable": {"thread_id": str(agent_id)}},
        ledger=LlmLedger(),
        rng=_rng(),
    )
    assert "compact" not in result.update  # type: ignore[operator]
    assert await _jobs(aops_pool) == []

from typing import Any, cast

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph import START, StateGraph
from psycopg_pool import AsyncConnectionPool

from agent import state as states
from base.agents.birth.spawn import create_agent_row
from base.agents.history.delta_read_compat import (
    recovery_reconstruction_scope,
    wrap_saver_reads_with_delta_reconstruction,
)
from base.cluster.machine import MachineIdentityReader
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.config.service_read import ConfigAuthority
from base.db import Database
from base.events.live.bus import EventBus
from base.lm.catalog import ModelCatalog
from services.agent_runner.agent_host.invocation.checkpoints import TurnCheckpoints
from services.agent_runner.agent_host.tests.runtime.host_support import BuildWith
from services.agent_runner.agent_host.tests.runtime.hosted_resources import hosted_scope


def test_unwrapped_saver_does_not_opt_into_reconstruction_cache() -> None:
    # meiva:note
    # A bare object has no attribute to read or call: the scope must leave it untouched.
    with recovery_reconstruction_scope(cast(Any, object()), "no-delta-wrapper") as scope:
        assert scope is None


def _no_update(_state: states.AgentState) -> dict[str, Any]:
    return {}


# meiva:note
# A saver that counts the delta-history reads a cold admission pays for.
class _CountingSaver(AsyncPostgresSaver):
    reads = 0

    async def aget_delta_channel_history(self, **kwargs: Any) -> Any:
        self.reads += 1
        return await super().aget_delta_channel_history(**kwargs)


@pytest.mark.parametrize("dangling", [False, True])
async def test_cold_admission_reads_the_history_once_and_never_repairs_it(
    aops_pool: AsyncConnectionPool,
    wired_with: BuildWith,
    dangling: bool,
    database: Database,
    event_bus: EventBus,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    machine_identity: MachineIdentityReader,
) -> None:
    agent, *_ = create_agent_row(
        database,
        event_bus,
        spawner="user",
        machine=machine_identity.name(),
        catalog=model_catalog,
        authority=config_authority,
    )
    saver = _CountingSaver(cast(Any, aops_pool))
    wrap_saver_reads_with_delta_reconstruction(saver)
    observed: list[Any] = []

    async def work(state: states.AgentState) -> dict[str, Any]:
        observed.extend(state.messages)
        return {"halted": True, "turn_idle": True}

    builder: Any = StateGraph(states.AgentState)
    builder.add_node("work", work)
    builder.add_edge(START, "work")
    builder.add_edge("work", "__end__")
    graph = builder.compile(checkpointer=saver)
    config: RunnableConfig = {"configurable": {"thread_id": str(agent)}}
    await graph.ainvoke({"messages": [HumanMessage(id="seed", content="Seed")]}, config=config)
    observed.clear()
    tail = (
        AIMessage(
            id="tail",
            content="",
            tool_calls=[{"id": "unfinished", "name": "execute_code", "args": {}}],
        )
        if dangling
        else HumanMessage(id="tail", content="Continue")
    )
    await graph.aupdate_state(config, {"messages": [tail]}, as_node="work")
    raw = await AsyncPostgresSaver.aget_tuple(saver, config)
    assert raw is not None and "messages" not in raw.checkpoint["channel_values"]
    saver.reads = 0
    host, _, _ = wired_with({}, graph, checkpointer=saver)

    async with hosted_scope() as resources:
        await host._run_turn(agent, resources=resources)

    assert saver.reads == 1
    assert not any(isinstance(m, ToolMessage) for m in observed)
    assert host.stats.cache_misses == 1
    await graph.aget_state(config)
    assert saver.reads == 2


async def test_a_turns_checkpoint_view_keeps_the_graph_and_binds_only_the_reader() -> None:
    saver = AsyncPostgresSaver(cast(Any, object()))
    wrap_saver_reads_with_delta_reconstruction(saver)
    builder: Any = StateGraph(states.AgentState)
    builder.add_node("work", _no_update)
    builder.add_edge(START, "work")
    builder.add_edge("work", "__end__")
    graph: Any = builder.compile(checkpointer=saver)

    with recovery_reconstruction_scope(saver, "view") as scope:
        view = TurnCheckpoints(saver, graph).bind(scope)
        bound: Any = view.graph

        assert bound.nodes == graph.nodes
        assert all(bound.nodes[key] is node for key, node in graph.nodes.items())
        assert bound.channels == graph.channels
        assert bound.checkpointer is view.saver
        assert view.saver.serde is saver.serde
        assert view.saver.aput is saver.aput

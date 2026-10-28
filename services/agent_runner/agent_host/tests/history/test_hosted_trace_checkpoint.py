"""A host turn's trace resolves its final, durably flushed conversation."""

import asyncio
from dataclasses import replace
from uuid import uuid4

import psycopg
import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph import END, START, StateGraph
from psycopg_pool import AsyncConnectionPool

from agent.impersonation_handoff import flush_checkpoint
from agent.startup import wrap_saver_writes_with_nstep_interval
from agent.state import BaseAgentState
from agent.tests.claim.test_inbound_ownership import admit_owner, agent_row
from base.agents.context import MeivaContext
from base.agents.history.checkpoint import load_checkpoint_messages_by_trace
from base.clock import SYSTEM_TIMERS, Clock
from base.clock.tests.seeded_rng import seeded_rng
from base.cluster.machine.configured import ConfiguredMachineIdentity
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.config import settings
from base.config.tests.configuration import defaults
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.host.env.agent_slices import AgentSlices
from base.lm.catalog import ModelCatalog
from base.packages.composer_commands import CommandCatalog
from base.paths import meiva_home
from base.telemetry.tests.capture.armed_tracing import armed_billing
from services.agent_runner.agent_host import host as host_module
from services.agent_runner.agent_host.tests.host_policy import configured_policy, handoff_timestamps
from services.agent_runner.agent_host.tests.runtime.hosted_resources import hosted_scope


async def test_host_trace_reads_final_messages_after_nstep_flush(
    aops_pool: AsyncConnectionPool,
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
    model_catalog: ModelCatalog,
    *,
    database_gate: ProcessDbGate,
    machine_identity: ConfiguredMachineIdentity,
) -> None:
    machine_identity.set(name="test")
    async with hosted_scope() as resources:
        agent_id = agent_row(db_conn)
        incarnation = await admit_owner(aops_pool, agent_id, database_gate=database_gate)

        def finish(state: BaseAgentState) -> dict[str, object]:
            assert len(state.messages) == 1
            return {
                "messages": [AIMessage(content="final response", id="final")],
                "halted": True,
                "turn_idle": True,
            }

        builder = StateGraph(BaseAgentState, context_schema=MeivaContext)
        builder.add_node("finish", finish)  # pyright: ignore[reportUnknownMemberType]
        builder.add_edge(START, "finish")
        builder.add_edge("finish", END)
        config: RunnableConfig = {"configurable": {"thread_id": str(agent_id)}}
        with armed_billing() as tracing:
            async with AsyncPostgresSaver.from_conn_string(settings.data_plane.db_url) as saver:
                wrap_saver_writes_with_nstep_interval(saver, 100)
                graph = builder.compile(checkpointer=saver)  # pyright: ignore[reportUnknownMemberType]
                await graph.aupdate_state(
                    config, {"messages": [HumanMessage(content="prior question", id="prior")]}
                )
                await flush_checkpoint(saver, agent_id)
                host = host_module.AgentHost(
                    runtime_owner=uuid4(),
                    handoff_stamping=handoff_timestamps(),
                    home=meiva_home(),
                    policy=configured_policy(),
                    pool=aops_pool,
                    checkpointer=saver,
                    graph=graph,
                    machine=machine_identity,
                    bus=EventBus.from_settings(rng=seeded_rng(), timers=SYSTEM_TIMERS),
                    db=Database.from_settings(gate=database_gate),
                    catalog=model_catalog,
                    commands=CommandCatalog.system(skills=list, home=meiva_home()),
                )
                assert not (
                    await host._invoke_until_done(
                        agent_id,
                        replace(
                            MeivaContext(
                                ops_pool=aops_pool,
                                agent=AgentSlices.resolve(default_reader=defaults()),
                                db=Database.from_settings(gate=database_gate),
                                bus=EventBus.from_settings(rng=seeded_rng(), timers=SYSTEM_TIMERS),
                                catalog=model_catalog,
                                clock_factory=Clock.from_settings,
                                machine=machine_identity,
                            ),
                            original_incarnation=incarnation,
                            hosted_resources=resources,
                        ),
                    )
                ).exited

        turn_spans = [
            span
            for span in tracing.exporter.get_finished_spans()
            if span.name == f"meiva-agent-{agent_id}"
        ]
        assert all(span.context is not None for span in turn_spans)
        traces = [format(span.context.trace_id, "032x") for span in turn_spans if span.context]
        assert len(traces) == 1
        # This is the actual gateway trace-content reader, using fresh connections.
        checkpoint_id, messages = await asyncio.to_thread(
            load_checkpoint_messages_by_trace,
            Database.from_settings(gate=database_gate),
            agent_id,
            traces[0],
        )
        assert checkpoint_id is not None
        assert [message.text for message in messages] == ["prior question", "final response"]

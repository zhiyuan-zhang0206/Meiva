# meiva:note
# A compaction abort settles as a live agent, not as a crash corpse.

import asyncio
import random
from uuid import uuid4

import psycopg
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg_pool import AsyncConnectionPool

from agent.graph import build_graph
from agent.graph.exec.tests.process_inputs import live_parent_facts
from agent.graph.hooks.compact import CompactionFailedError
from agent.ownership.hosted import admit_hosted_runtime
from agent.state import BaseAgentState
from agent.turn.runloop import PendingTurnFailure
from base.agents.context import MeivaContext
from base.agents.messages.kwargs import NoteTag, read_meiva_kwargs
from base.agents.tests.units import spawn_agent
from base.cluster.machine import MachineIdentityReader
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.config.service_read import ConfigAuthority
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.events.live.publisher import AgentEventPublisher
from base.host.env.agent_slices import AgentSlices
from base.lm.catalog import ModelCatalog
from base.packages.plugins.extensions import ExtensionRegistry
from services.agent_runner.agent_host import settlement
from services.agent_runner.agent_host.invocation import finish_pending_failure
from services.agent_runner.agent_host.tests.host_policy import configured_policy


# meiva:note
# A fresh seeded generator for each use, so no test sees another's draws.
def _rng() -> random.Random:
    return random.Random(0)  # noqa: S311


# meiva:note
# The auto path's `CompactionFailedError` aborts the turn with the history preserved. The agent idles: nothing stamps `last_turn_fatal_at`, so the corpse reaper never terminates it, and the turn's Error still reaches the user once the row is settled.
async def test_compaction_abort_leaves_no_corpse_marker_and_still_reports(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    event_bus: EventBus,
    database: Database,
    db_url: str,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentityReader,
) -> None:
    agent_id = spawn_agent(
        catalog=model_catalog,
        authority=config_authority,
        database_gate=database_gate,
        machine=machine_identity,
    )
    machine = db_conn.execute("SELECT machine FROM agents_meta WHERE id=%s", (agent_id,)).fetchone()
    assert machine is not None
    incarnation = await admit_hosted_runtime(aops_pool, agent_id, machine[0], uuid4(), db=database)
    assert incarnation is not None
    claimed = db_conn.execute(
        "INSERT INTO inbound_messages(agent_id,content,kind,source,status) "
        "VALUES(%s,'already delivered','chat','user','claimed') RETURNING id",
        (agent_id,),
    ).fetchone()
    db_conn.commit()
    assert claimed is not None
    pending = PendingTurnFailure(CompactionFailedError("no usable summary"))
    publisher = AgentEventPublisher(
        event_bus.async_redis(),
        event_bus.channel,
        agent_id=agent_id,
        auth_retry=event_bus.auth_retry,
    )
    ctx = MeivaContext(
        ops_pool=aops_pool,
        event_publisher=publisher,
        bus=event_bus,
        db=database,
        catalog=model_catalog,
        agent=AgentSlices.resolve(default_reader=configured_policy().default_reader),
        clock_factory=configured_policy().clock_factory,
        original_incarnation=incarnation,
    )
    async with AsyncPostgresSaver.from_conn_string(db_url) as saver:
        graph = build_graph(
            saver,
            ExtensionRegistry(),
            parent=live_parent_facts(),
            rng=_rng(),
        )
        config: RunnableConfig = {"configurable": {"thread_id": str(agent_id)}}
        async with asyncio.TaskGroup() as workers:
            await publisher.start(workers)
            initial_turn = graph.with_types(input_type=BaseAgentState, output_type=BaseAgentState)
            await initial_turn.ainvoke(
                BaseAgentState(halted=True), config, context=ctx, interrupt_before=["init_context"]
            )
            outcome = await finish_pending_failure(graph, saver, agent_id, ctx, config, pending)
            assert outcome.aborted and not outcome.crashed
            assert len(pending.errors) == 1
            persisted = (await graph.aget_state(config)).values
            [note] = persisted["messages"]
            assert read_meiva_kwargs(note).get("meiva_note_tag") == NoteTag.COMPACT_FAILED.value
            assert "no usable summary" in note.content
            assert persisted["halted"] is True
            await settlement.close_hosted_turn(
                aops_pool, aops_pool, event_bus, incarnation, outcome
            )
            await publisher.aclose()
    assert outcome.failure_report is None and pending.errors == []
    assert db_conn.execute(
        "SELECT status, last_turn_fatal_at IS NULL FROM agents_meta WHERE id=%s", (agent_id,)
    ).fetchone() == ("idling", True)
    assert db_conn.execute(
        "SELECT status FROM inbound_messages WHERE id=%s", (claimed[0],)
    ).fetchone() == ("done",)

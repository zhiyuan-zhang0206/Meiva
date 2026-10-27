import os
import random
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import psycopg
import pytest
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph import START, StateGraph
from psycopg_pool import AsyncConnectionPool

from agent import state as states
from agent.ownership.hosted import admit_hosted_runtime, settle_running_rows_at_start
from base.agents.context import MeivaContext
from base.agents.history.delta_read_compat import wrap_saver_reads_with_delta_reconstruction
from base.agents.messages.timestamp_policy import TimestampSource
from base.agents.observation.relay_supervision import RelaySupervision
from base.clock import SYSTEM_TIMERS, Clock, ClockConfig
from base.cluster.machine import MachineIdentityReader
from base.db import Database, create_agent
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.host.env.agent_slices import AgentSlices
from base.lm.catalog import ModelCatalog
from base.paths import meiva_home
from services.agent_runner.agent_host.invocation import (
    HandoffSettlement,
    settle_handoff_before_invocation,
)
from services.agent_runner.agent_host.scheduling.pending_wakes import scan_rows
from services.agent_runner.agent_host.tests.host_policy import configured_policy


# meiva:note
# A fresh seeded generator for each use, so no test sees another's draws.
def _rng() -> random.Random:
    return random.Random(0)  # noqa: S311


@pytest.mark.parametrize(
    ("status", "receipt", "deltas", "expected"),
    [
        ("requested", False, False, True),
        ("expired", False, False, True),
        ("rejected", False, False, True),
        ("released", True, True, True),
        ("released", True, False, False),
    ],
)
async def test_cold_host_finishes_takeover_without_an_inbound(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    status: str,
    receipt: bool,
    deltas: bool,
    expected: bool,
) -> None:
    agent_id = create_agent(db_conn)
    db_conn.execute(
        "INSERT INTO agents_meta(id,status,machine) VALUES(%s,'idling','handoff-scan')", (agent_id,)
    )
    db_conn.execute(
        "INSERT INTO agent_impersonations(id,agent_id,source,machine,status,ttl_seconds,expires_at,"
        "handoff_applied_at,plugin_delta,delta_version) "
        "VALUES(%s,%s,'external_agent:codex','handoff-scan',%s,300,clock_timestamp(),"
        "CASE WHEN %s THEN clock_timestamp() END,CASE WHEN %s THEN '[{}]'::jsonb ELSE '[]'::jsonb END,%s)",
        (uuid4(), agent_id, status, receipt, deltas, int(deltas)),
    )
    db_conn.commit()
    assert db_conn.execute("SELECT count(*) FROM inbound_messages").fetchone() == (0,)
    assert (agent_id in await scan_rows(aops_pool, "handoff-scan")) is expected


# meiva:note
# Selection is not recovery: what the scan is for is the turn its wake produces, and that turn
# writes the receipt. This is the restart sequence end to end — the boot settle idles the row
# without a crash marker, the scan selects it, admission takes it, and the turn's first step
# settles the checkpoint — so a future narrowing on either side of that seam fails here (#5623).
async def test_the_scan_wake_settles_the_receipt_it_selected_the_row_for(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool[Any],
    database: Database,
    event_bus: EventBus,
    model_catalog: ModelCatalog,
    *,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentityReader,
) -> None:
    agent_id = create_agent(db_conn)
    db_conn.execute(
        "INSERT INTO agents_meta(id,status,machine,runtime_generation,runtime_owner) "
        "VALUES(%s,'running','handoff-scan',%s,%s)",
        (agent_id, uuid4(), uuid4()),
    )
    db_conn.execute(
        "INSERT INTO agent_impersonations(id,agent_id,source,machine,status,ttl_seconds,"
        "expires_at,ended_at) VALUES(%s,%s,'external_agent:codex','handoff-scan','released',300,"
        "clock_timestamp(),clock_timestamp())",
        (uuid4(), agent_id),
    )
    db_conn.commit()

    assert await settle_running_rows_at_start(aops_pool, "handoff-scan") == [agent_id]
    # meiva:note
    # The settle leaves no crash marker, and the conventions say so when they call the scan
    # the path a restart takes. A stamp added here "for safety" would falsify that sentence
    # while every other assertion below still passed, so pin the absence itself.
    assert db_conn.execute(
        "SELECT last_turn_fatal_at FROM agents_meta WHERE id=%s", (agent_id,)
    ).fetchone() == (None,)
    assert await scan_rows(aops_pool, "handoff-scan") == [agent_id]

    async def idle(_state: states.BaseAgentState) -> dict[str, Any]:
        return {}

    saver = AsyncPostgresSaver(aops_pool)
    await saver.setup()
    wrap_saver_reads_with_delta_reconstruction(saver)
    builder: Any = StateGraph(states.AgentState, context_schema=MeivaContext)
    builder.add_node("idle", idle)
    builder.add_edge(START, "idle")
    graph = builder.compile(checkpointer=saver)
    incarnation = await admit_hosted_runtime(
        aops_pool,
        agent_id,
        "handoff-scan",
        uuid4(),
        db=Database.from_settings(gate=database_gate),
    )
    assert incarnation is not None
    policy = configured_policy()
    await settle_handoff_before_invocation(
        graph,
        MeivaContext(
            ops_pool=aops_pool,
            original_incarnation=incarnation,
            hosted_resources=None,
            agent=AgentSlices.resolve(default_reader=policy.default_reader),
            machine=machine_identity,
            db=database,
            bus=event_bus,
            event_publisher=MagicMock(),
            catalog=model_catalog,
            clock_factory=policy.clock_factory,
        ),
        agent_id,
        fresh_input=False,
        settlement=HandoffSettlement(
            db=database,
            bus=event_bus,
            relays=RelaySupervision(),
            timestamps=TimestampSource(
                clock=Clock(ClockConfig("UTC", "UTC", False), timers=SYSTEM_TIMERS),
                timestamps_enabled=False,
            ),
            local_machine="handoff-scan",
            home=meiva_home(),
            rng=_rng(),
            environ=dict(os.environ),
        ),
    )
    assert db_conn.execute(
        "SELECT handoff_applied_at IS NOT NULL FROM agent_impersonations WHERE agent_id=%s",
        (agent_id,),
    ).fetchone() == (True,)

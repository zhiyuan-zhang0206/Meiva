# meiva:note
# A Claude turn that did something and has no record file to show for it fails the way a harness setup failure does, and the
# runloop reports it as it reports any fatal turn: a live `Error` event, on the page of the moment and nowhere
# after it (a built-in agent's turn failure is shown the same way).
# meiva:link docs/decisions/agents/graph/2026-11-13-timeline-read-from-the-record.md

from __future__ import annotations

import json
import re
from dataclasses import replace
from pathlib import Path

import psycopg
import pytest
from langgraph.runtime import Runtime
from psycopg_pool import AsyncConnectionPool

from agent.graph.claim.harness_turn import harness_turn_node
from agent.graph.claim.tests.harness_support import (
    FakeEngine,
    config,
    in_a_turn,
    insert_row,
    runtime,
)
from agent.graph.claim.tests.harness_support import hosted_resources as hosted_resources
from agent.graph.llm_errors import FatalLLMStreamError
from agent.ownership.tests.ownership_support import agent_row
from agent.state import AgentState
from agent.turn.runloop import handle_fatal_llm_error
from base.agents.context import HostedTurnResources
from base.api_contracts.agent_contract import Harness
from base.clock import SYSTEM_TIMERS
from base.clock.tests.seeded_rng import seeded_rng
from base.config.tests.configuration import defaults
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.events.live.tests.live_capture import LiveEvents
from base.events.live.tests.live_capture import live_events as live_events
from base.host.env.agent_slices import AgentSlices

SESSION = "5914ebdf-bbec-47c8-bcd6-5a6da081f08a"


async def test_a_turn_with_no_record_file_names_the_session_and_where_it_looked_in_a_live_error(
    tmp_path: Path,
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    database_gate: ProcessDbGate,
    hosted_resources: HostedTurnResources,
    live_events: LiveEvents,
) -> None:
    agent_id = agent_row(db_conn)
    _ = insert_row(db_conn, agent_id, "chat", "user", "hello")
    engine = FakeEngine(script=lambda inputs: in_a_turn([i.key for i in inputs], "an unseen reply"))
    engine.session_id = SESSION
    base = runtime(
        engine,
        ops_pool=aops_pool,
        hosted_resources=hosted_resources,
        publisher=live_events.publisher,
    )
    rt = Runtime(
        context=replace(
            base.context,
            harness=Harness.CLAUDE_CODE,
            meiva_home=tmp_path / "home",
            environ={"HOME": str(tmp_path)},
            db=Database.from_settings(gate=database_gate),
            bus=EventBus.from_settings(rng=seeded_rng(), timers=SYSTEM_TIMERS),
            agent=AgentSlices.resolve({"user_delivery_mode": "naive"}, default_reader=defaults()),
        )
    )

    with pytest.raises(FatalLLMStreamError) as failed:
        _ = await harness_turn_node(AgentState(), rt, config(agent_id))
    _ = await handle_fatal_llm_error(failed.value, rt.context, agent_id, circuit=None)

    errors = [e for e in map(json.loads, await live_events.payloads()) if e.get("role") == "error"]
    [content] = [e["content"] for e in errors]
    assert SESSION in content and str(tmp_path / ".claude/projects") in content
    assert re.search(r"was not found in .*projects", content)

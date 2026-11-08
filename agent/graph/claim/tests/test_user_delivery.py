from dataclasses import replace

import psycopg
import pytest
from langgraph.runtime import Runtime
from psycopg_pool import AsyncConnectionPool

from agent.graph.claim.harness_turn import harness_turn_node
from agent.graph.claim.tests.test_harness_turn import FakeEngine, config, runtime
from agent.graph.llm_errors import FatalLLMStreamError
from agent.state import AgentState
from base.agents.context import HostedTurnResources
from base.agents.history.harness_messages import aload_harness_timeline_items
from base.config.tests.configuration import defaults
from base.db import Database, create_agent
from base.db.code_version_gate import ProcessDbGate
from base.events.live.tests.live_capture import LiveEvents
from base.host.env.agent_slices import AgentSlices
from meiva.tests.fixtures.pin_agent import hosted_resources as hosted_resources


async def test_sdk_harness_corrects_only_at_natural_end_and_keeps_raw_final_in_details(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    database_gate: ProcessDbGate,
    hosted_resources: HostedTurnResources,
    live_events: LiveEvents,
) -> None:
    aid = create_agent(db_conn)
    db_conn.execute(
        "INSERT INTO agents_meta (id, status, harness) VALUES (%s, 'running', 'claude_code')",
        (aid,),
    )
    db_conn.commit()
    engine = FakeEngine(reply="A private final summary")
    base = runtime(
        engine,
        ops_pool=aops_pool,
        hosted_resources=hosted_resources,
        publisher=live_events.publisher,
    )
    sdk = Runtime(
        context=replace(
            base.context,
            db=Database.from_settings(gate=database_gate),
            agent=AgentSlices.resolve({"user_delivery_mode": "sdk"}, default_reader=defaults()),
        )
    )
    state = AgentState(
        harness_pending_input={"prompt": "Do the work", "key": "human-request"},
        user_delivery_baseline=0,
    )
    first = await harness_turn_node(state, sdk, config(aid))
    assert first.goto == "harness_turn"
    update = AgentState.model_validate(first.update)
    assert update.harness_pending_input is not None
    assert update.harness_pending_input["key"] == "human-request:visibility-correction"
    async with aops_pool.connection() as conn:
        items = await aload_harness_timeline_items(conn, aid)
    raw = [item for item in items if item.kind == "agent_chat"]
    assert len(raw) == 1 and raw[0].user_facing is False
    corrected = AgentState(
        harness_pending_input=update.harness_pending_input, user_delivery_baseline=0
    )
    with pytest.raises(FatalLLMStreamError, match="SDK delivery contract violated"):
        await harness_turn_node(corrected, sdk, config(aid))
    assert engine.interrupts == 0


async def test_harness_turn_does_not_inject_visibility_rule_and_triggers_correction_on_violation() -> (
    None
):
    engine_valid = FakeEngine(reply="MEIVA_DISPLAY_TO_USER\nhello")
    state = AgentState(harness_pending_input={"prompt": "hello from user", "key": "inbound-10"})
    cmd_valid = await harness_turn_node(state, runtime(engine_valid), config())
    assert cmd_valid.goto == "claim"
    assert cmd_valid.update is not None
    assert cmd_valid.update["harness_pending_input"] is None
    [(prompt, _)] = engine_valid.submitted
    assert prompt == "hello from user"

    engine_invalid = FakeEngine(reply="unprefixed reply")
    cmd_invalid = await harness_turn_node(state, runtime(engine_invalid), config())
    assert cmd_invalid.goto == "harness_turn"
    assert cmd_invalid.update is not None
    update = AgentState.model_validate(cmd_invalid.update)
    assert update.harness_pending_input is not None
    assert update.harness_pending_input["visibility_correction"] is True
    assert update.harness_pending_input["key"] == "inbound-10:visibility-correction"
    assert "visibility prefix" in str(update.harness_pending_input["prompt"])

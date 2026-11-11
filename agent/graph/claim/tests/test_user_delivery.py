from dataclasses import replace

import psycopg
import pytest
from langgraph.runtime import Runtime
from psycopg_pool import AsyncConnectionPool

from agent.graph.claim.harness_turn import harness_turn_node
from agent.graph.claim.tests.harness_support import FakeEngine
from agent.graph.claim.tests.test_harness_turn import config, runtime
from agent.graph.llm_errors import FatalLLMStreamError
from agent.state import AgentState
from base.agents.context import HostedTurnResources
from base.agents.history.harness_messages import aload_harness_timeline_items
from base.clock import SYSTEM_TIMERS
from base.clock.tests.seeded_rng import seeded_rng
from base.config.tests.configuration import defaults
from base.db import Database, create_agent
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.events.live.tests.live_capture import LiveEvents
from base.host.env.agent_slices import AgentSlices
from base.lm.harness import AssistantText, TextDelta
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
        harness_inputs=[{"key": "human-request", "prompt": "Do the work", "timeline_items": []}],
        user_delivery_baseline=0,
    )
    with pytest.raises(FatalLLMStreamError, match="SDK delivery contract violated"):
        await harness_turn_node(state, sdk, config(aid))
    [first, second] = engine.delivered
    assert [item.key for item in first] == ["human-request"]
    assert [item.key for item in second] == ["human-request:visibility-correction"]
    async with aops_pool.connection() as conn:
        items = await aload_harness_timeline_items(conn, aid)
    raw = [item for item in items if item.kind == "agent_chat"]
    assert len(raw) == 2 and all(item.user_facing is False for item in raw)
    assert engine.interrupts == 0


async def test_harness_turn_does_not_inject_visibility_rule_and_corrects_once_on_violation() -> (
    None
):
    engine_valid = FakeEngine(reply="MEIVA_DISPLAY_TO_USER\nhello")
    state = AgentState(
        harness_inputs=[{"key": "inbound-10", "prompt": "hello from user", "timeline_items": []}]
    )
    cmd_valid = await harness_turn_node(state, runtime(engine_valid), config())
    assert cmd_valid.goto == "claim"
    assert cmd_valid.update is not None
    assert cmd_valid.update["harness_inputs"] == []
    [(prompt, _)] = engine_valid.submitted
    assert prompt == "hello from user"

    engine_invalid = FakeEngine(reply="unprefixed reply")
    cmd_invalid = await harness_turn_node(state, runtime(engine_invalid), config())
    assert cmd_invalid.goto == "claim"
    [(original, _), (correction, correction_key)] = engine_invalid.submitted
    assert original == "hello from user"
    assert correction_key == "inbound-10:visibility-correction"
    assert "visibility prefix" in correction


# meiva:note
# A turn cut off mid-reply (interrupt) still leaves the text the reader was shown: it is sealed
# as it stood and published once, under the key of its timeline item, like a completed reply.
# meiva:link agent/graph/docs/harness-turn.md
@pytest.mark.parametrize("interrupted", [True, False])
async def test_harness_reply_is_published_once_whether_or_not_the_turn_was_cut_off(
    interrupted: bool,
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
    deltas = (
        TextDelta(turn_id="t1", text="Half a "),
        TextDelta(turn_id="t1", text="reply"),
    )
    engine = FakeEngine(
        reply="" if interrupted else "Half a reply",
        events=deltas
        if interrupted
        else (*deltas, AssistantText(turn_id="t1", text="Half a reply")),
        interrupted=interrupted,
    )
    base = runtime(
        engine,
        ops_pool=aops_pool,
        hosted_resources=hosted_resources,
        publisher=live_events.publisher,
    )
    naive = Runtime(
        context=replace(
            base.context,
            db=Database.from_settings(gate=database_gate),
            bus=EventBus.from_settings(rng=seeded_rng(), timers=SYSTEM_TIMERS),
            agent=AgentSlices.resolve({"user_delivery_mode": "naive"}, default_reader=defaults()),
        )
    )
    state = AgentState(
        harness_inputs=[{"key": "human-request", "prompt": "Work", "timeline_items": []}]
    )
    await harness_turn_node(state, naive, config(aid))

    async with aops_pool.connection() as conn:
        items = await aload_harness_timeline_items(conn, aid)
    [chat] = [item for item in items if item.kind == "agent_chat"]
    assert chat.payload == "Half a reply" and chat.activity_pending is False
    assert db_conn.execute(
        "SELECT source_key, content FROM agent_user_messages WHERE agent_id=%s", (aid,)
    ).fetchall() == [(f"harness:{chat.item_id}", "Half a reply")]

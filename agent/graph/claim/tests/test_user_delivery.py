from dataclasses import replace

import psycopg
import pytest
from langgraph.runtime import Runtime
from psycopg_pool import AsyncConnectionPool

from agent.graph.claim.harness_activity import (
    SUBAGENT_CUT,
    SUBAGENT_ITEM_CHARS,
)
from agent.graph.claim.harness_turn import harness_turn_node
from agent.graph.claim.tests.harness_support import FakeEngine
from agent.graph.claim.tests.test_harness_turn import config, runtime
from agent.graph.llm_errors import FatalLLMStreamError
from agent.state import AgentState
from base.agents.context import HostedTurnResources
from base.agents.history.harness_messages import aload_harness_timeline_items
from base.agents.history.timeline_item import TimelineItem
from base.clock import SYSTEM_TIMERS
from base.clock.tests.seeded_rng import seeded_rng
from base.config.tests.configuration import defaults
from base.db import Database, create_agent
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.events.live.tests.live_capture import LiveEvents
from base.host.env.agent_slices import AgentSlices
from base.lm.harness import (
    AssistantText,
    BridgeEvent,
    SubagentText,
    TextDelta,
    ToolResult,
    ToolUse,
)
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
        "SELECT source_key, content FROM agent_publications WHERE agent_id=%s", (aid,)
    ).fetchall() == [(f"harness:{chat.item_id}", "Half a reply")]


def _said(text: str, *, call: str = "call-agent", agent: str | None = None) -> SubagentText:
    return SubagentText(turn_id="t1", tool_use_id=call, agent=agent, text=text)


# meiva:note
# One delivery-mode "naive" turn in which the agent launches a sub-agent and the CLI reports what it
# said, run against a real database; the timeline items and the agent's delivery rows come back.
# meiva:link base/lm/harness/docs/subagent-text.md
async def _subagent_turn(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    database_gate: ProcessDbGate,
    hosted_resources: HostedTurnResources,
    live_events: LiveEvents,
    said: tuple[BridgeEvent, ...],
    *,
    agent_id: int | None = None,
    launching: bool = True,
) -> tuple[list[TimelineItem], list[tuple[object, ...]]]:
    aid = agent_id if agent_id is not None else create_agent(db_conn)
    if agent_id is None:
        db_conn.execute(
            "INSERT INTO agents_meta (id, status, harness) VALUES (%s, 'running', 'claude_code')",
            (aid,),
        )
        db_conn.commit()
    launch = ToolUse(
        turn_id="t1", tool_use_id="call-agent", name="Agent", tool_input={"prompt": "go"}
    )
    launched = ToolResult(
        turn_id="t1", tool_use_id="call-agent", content="launched", is_error=False
    )
    engine = FakeEngine(
        reply="MEIVA_DISPLAY_TO_USER\nthe answer",
        events=(launch, launched, *said) if launching else said,
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
    state = AgentState(harness_inputs=[{"key": "req", "prompt": "Work", "timeline_items": []}])
    await harness_turn_node(state, naive, config(aid))
    async with aops_pool.connection() as conn:
        items = await aload_harness_timeline_items(conn, aid)
    rows = db_conn.execute(
        "SELECT source_key, content FROM agent_publications WHERE agent_id=%s", (aid,)
    ).fetchall()
    return items, rows


# meiva:note
# A CLI sub-agent's text is work detail: it lands on the timeline as `subagent_text` items tied to the
# spawning call, never as a reply, so it writes no delivery row (nothing unread, nothing pushed).
# meiva:link base/lm/harness/docs/subagent-text.md
async def test_subagent_text_is_timeline_detail_and_never_a_delivery(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    database_gate: ProcessDbGate,
    hosted_resources: HostedTurnResources,
    live_events: LiveEvents,
) -> None:
    said = (
        _said("ALPHA", agent="general-purpose"),
        _said("orphan", call="unseen-call"),
        _said("   "),
    )
    items, rows = await _subagent_turn(
        db_conn, aops_pool, database_gate, hosted_resources, live_events, said
    )
    call = next(item for item in items if item.kind == "agent_code")
    detail = [
        (i.payload, i.source, i.parent_item_id, i.user_facing)
        for i in items
        if i.kind == "subagent_text"
    ]
    assert detail == [
        ("ALPHA", "general-purpose", call.item_id, False),
        ("orphan", None, None, False),
    ]
    reply = next(i for i in items if i.kind == "agent_chat")
    assert rows == [(f"harness:{reply.item_id}", "MEIVA_DISPLAY_TO_USER\nthe answer")]


# meiva:note
# Sub-agent text is cut per message; every message is still written.
# meiva:link base/lm/harness/docs/subagent-text.md
async def test_subagent_text_is_cut_per_message_and_every_message_is_written(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    database_gate: ProcessDbGate,
    hosted_resources: HostedTurnResources,
    live_events: LiveEvents,
) -> None:
    long_text = "x" * (SUBAGENT_ITEM_CHARS + 50)
    said = (_said(long_text), _said("y" * SUBAGENT_ITEM_CHARS), _said("later"))
    items, _ = await _subagent_turn(
        db_conn, aops_pool, database_gate, hosted_resources, live_events, said
    )
    texts = [i.payload for i in items if i.kind == "subagent_text"]
    cut = texts[0]
    assert texts[1:] == ["y" * SUBAGENT_ITEM_CHARS, "later"]
    assert cut.endswith(SUBAGENT_CUT) and len(cut) < len(long_text)


def _subagent_call(call: str, name: str = "Bash", *, parent: str = "call-agent") -> ToolUse:
    return ToolUse(
        turn_id="t1",
        tool_use_id=call,
        name=name,
        tool_input={"command": "echo hi"},
        parent_tool_use_id=parent,
    )


def _subagent_result(call: str, content: str, *, parent: str = "call-agent") -> ToolResult:
    return ToolResult(
        turn_id="t1", tool_use_id=call, is_error=False, content=content, parent_tool_use_id=parent
    )


# meiva:note
# A tool call the sub-agent made lands as the usual `agent_code` / `code_output` pair, tied to the
# `agent_code` item of the call that started the sub-agent; the main agent's own calls stay untied.
# meiva:link base/lm/harness/docs/subagent-text.md
async def test_a_subagents_tool_activity_is_tied_to_the_call_that_started_it(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    database_gate: ProcessDbGate,
    hosted_resources: HostedTurnResources,
    live_events: LiveEvents,
) -> None:
    said = (
        _said("ALPHA", agent="general-purpose"),
        _subagent_call("sub-bash"),
        _subagent_result("sub-bash", "hi"),
        _subagent_call("lost-bash", parent="unknown-call"),
    )
    items, rows = await _subagent_turn(
        db_conn, aops_pool, database_gate, hosted_resources, live_events, said
    )
    spawn = next(i for i in items if i.tool_use_id == "call-agent")
    assert spawn.parent_item_id is None
    parents = {i.tool_use_id: i.parent_item_id for i in items if i.kind == "agent_code"}
    assert parents == {"call-agent": None, "sub-bash": spawn.item_id, "lost-bash": None}, (
        "a call whose parent is nowhere stays unattributed"
    )
    outputs = {i.payload: i.parent_item_id for i in items if i.kind == "code_output"}
    assert outputs["launched"] is None and outputs["hi"] == spawn.item_id
    reply = next(i for i in items if i.kind == "agent_chat")
    assert rows == [(f"harness:{reply.item_id}", "MEIVA_DISPLAY_TO_USER\nthe answer")]


# meiva:note
# A sub-agent's tool output is cut like its text, and later calls are still written.
# meiva:link base/lm/harness/docs/subagent-text.md
async def test_a_subagents_tool_output_is_cut(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    database_gate: ProcessDbGate,
    hosted_resources: HostedTurnResources,
    live_events: LiveEvents,
) -> None:
    said = (
        _subagent_call("big"),
        _subagent_result("big", "z" * (SUBAGENT_ITEM_CHARS + 10)),
        _subagent_call("after"),
        _subagent_result("after", "late"),
    )
    items, _ = await _subagent_turn(
        db_conn, aops_pool, database_gate, hosted_resources, live_events, said
    )
    [big] = [i for i in items if i.kind == "code_output" and i.payload.startswith("z")]
    assert big.payload.endswith(SUBAGENT_CUT) and len(big.payload) < SUBAGENT_ITEM_CHARS + 100
    assert "late" in [i.payload for i in items]
    assert not [i for i in items if i.payload == "Tool running"], "no call is left running"


# meiva:note
# A sub-agent that keeps working after the invocation that saw its spawning call ended is still tied to
# that call: the call's timeline item is found by the CLI's call id.
# meiva:link base/lm/harness/docs/subagent-text.md
async def test_activity_after_the_launching_invocation_still_finds_its_call(
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
    await _subagent_turn(
        db_conn, aops_pool, database_gate, hosted_resources, live_events, (), agent_id=aid
    )
    items, _ = await _subagent_turn(
        db_conn,
        aops_pool,
        database_gate,
        hosted_resources,
        live_events,
        (
            _said("LATE"),
            _said("nobody's", call="never-seen"),
            _subagent_call("late-bash"),
            _subagent_result("late-bash", "hi"),
        ),
        agent_id=aid,
        launching=False,
    )
    [spawn] = [i for i in items if i.tool_use_id == "call-agent"]
    [late] = [i for i in items if i.payload == "LATE"]
    [nobody] = [i for i in items if i.payload == "nobody's"]
    [late_code] = [i for i in items if i.tool_use_id == "late-bash"]
    assert (late.parent_item_id, late_code.parent_item_id) == (spawn.item_id, spawn.item_id)
    assert nobody.parent_item_id is None

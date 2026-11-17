# meiva:note
# A refusal at the old turn's terminal boundary ends that delivery run. The inbox owns the retry,
# so the next run consumes the same row once without losing or duplicating its timeline entry.
# meiva:link agent/graph/docs/harness-foreground.md

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from pathlib import Path

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from agent.graph.claim.harness_read_sink import ReadTimelineSink
from agent.graph.claim.harness_turn import harness_turn_node
from agent.graph.claim.tests.harness_support import (
    FakeEngine,
    RuntimeFixture,
    completed,
    in_a_turn,
    input_key,
    insert_row,
    row_status,
    timeline_inbound_ids,
    wait_until,
)
from agent.graph.claim.tests.harness_support import harness_runtime as harness_runtime
from agent.graph.claim.tests.test_harness_turn import config
from agent.graph.claim.tests.test_harness_turn import hosted_resources as hosted_resources
from agent.ownership.tests.ownership_support import agent_row
from agent.state import AgentState
from base.agents.context import MeivaContext
from base.api_contracts.agent_contract import Harness
from base.config.tests.configuration import defaults
from base.events.live.projection import TimelineSnapshot
from base.events.live.tests.live_capture import LiveEvents
from base.events.live.tests.live_capture import live_events as live_events
from base.host.env.agent_slices import AgentSlices
from base.lm.harness import (
    BridgeEvent,
    HarnessInput,
    InputInContext,
    InputRejected,
    TurnOrigin,
    TurnStarted,
)
from base.lm.harness.antigravity.timing_store import store_timings, timing_path
from base.lm.harness.antigravity.trajectory import parse_trajectory


async def test_a_turn_boundary_rejection_hands_the_input_to_the_next_run_once(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    harness_runtime: RuntimeFixture,
) -> None:
    agent_id = agent_row(db_conn)
    first = insert_row(db_conn, agent_id, "chat", "user", "finish this turn")

    def at_the_boundary(inputs: Sequence[HarnessInput]) -> Sequence[BridgeEvent]:
        [entry] = inputs
        if entry.key == input_key(first):
            return []
        if engine.subscribe_count == 1:
            return [
                InputRejected(
                    entry.key,
                    "the running turn has finished; hand the input over again",
                    1,
                ),
                completed("t1", "MEIVA_DISPLAY_TO_USER\nfirst done"),
            ]
        return in_a_turn([entry.key], "MEIVA_DISPLAY_TO_USER\nsecond done", turn_id="t2")

    engine = FakeEngine(script=at_the_boundary)
    rt = harness_runtime(engine, agent_id=agent_id)
    async with asyncio.timeout(5), asyncio.TaskGroup() as tasks:
        first_run = tasks.create_task(harness_turn_node(AgentState(), rt, config(agent_id)))
        await wait_until(lambda: len(engine.delivered) == 1, "first delivery")
        engine.emit(
            TurnStarted("t1", TurnOrigin.INPUT, (input_key(first),), 1),
            InputInContext(input_key(first), "t1", 1),
        )
        second = insert_row(db_conn, agent_id, "chat", "user", "start the next turn")
        engine.notify_inbound()
        await first_run

    assert [row_status(db_conn, i) for i in (first, second)] == ["done", "pending"]
    second_deliveries = [
        item.key for batch in engine.delivered for item in batch if item.key == input_key(second)
    ]
    assert second_deliveries == [input_key(second)], "the finishing run makes one attempt"

    async with asyncio.timeout(5):
        _ = await harness_turn_node(AgentState(), rt, config(agent_id))

    assert row_status(db_conn, second) == "done"
    second_deliveries = [
        item.key for batch in engine.delivered for item in batch if item.key == input_key(second)
    ]
    assert second_deliveries == [input_key(second), input_key(second)]
    assert (await timeline_inbound_ids(aops_pool, agent_id)).count(second) == 1


@pytest.mark.parametrize("preexisting_cache", [False, True])
async def test_timing_revision_emits_only_same_id_reasoning_updates(
    tmp_path: Path,
    live_events: LiveEvents,
    preexisting_cache: bool,
) -> None:
    session = "native-timing"
    path = (
        tmp_path
        / ".gemini/antigravity-cli/brain"
        / session
        / ".system_generated/logs/transcript_full.jsonl"
    )
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "type": "PLANNER_RESPONSE",
                "status": "DONE",
                "step_index": 0,
                "created_at": "2026-11-17T05:30:00Z",
                "thinking": "synthetic thought",
                "content": "MEIVA_DISPLAY_TO_USER\nanswer",
                "tool_calls": [{"name": "run_command", "args": {"CommandLine": "echo fixture"}}],
            }
        )
        + "\n"
    )
    native = parse_trajectory(
        {
            "status": "CASCADE_RUN_STATUS_IDLE",
            "numTotalSteps": 1,
            "trajectory": {
                "steps": [
                    {
                        "type": "CORTEX_STEP_TYPE_PLANNER_RESPONSE",
                        "status": "CORTEX_STEP_STATUS_DONE",
                        "metadata": {"createdAt": "2026-11-17T05:30:00.123Z"},
                        "plannerResponse": {
                            "thinking": "synthetic thought",
                            "thinkingDuration": "30.265829s",
                        },
                    }
                ]
            },
        }
    )
    if preexisting_cache:
        store_timings(path, native)
    ctx = MeivaContext(
        agent=AgentSlices.resolve(default_reader=defaults()),
        harness=Harness.ANTIGRAVITY,
        meiva_home=tmp_path,
        environ={"HOME": str(tmp_path)},
    )
    sink = ReadTimelineSink(
        ctx, 100, live_events.publisher, non_human_wake=False, session_id=lambda: session
    )
    initial, end = sink._project_new()
    assert initial is not None
    old = next(item.item for item in initial.items if item.item.kind == "agent_reasoning")
    assert old.reasoning_ms == (30265 if preexisting_cache else None)
    assert await live_events.events() == []
    if not preexisting_cache:
        store_timings(path, native)
        await sink._refresh_timing(end)
        assert_reasoning_update(await live_events.typed(), old.item_id, 30265)
    assert sink.has_activity is False
    await sink._refresh_timing(end)
    assert await live_events.events() == []
    timing_path(path).unlink()
    await sink._refresh_timing(end)
    assert_reasoning_update(await live_events.typed(), old.item_id, None)
    await sink._refresh_timing(end)
    assert await live_events.events() == []


def assert_reasoning_update(events: Sequence[object], item_id: str, ms: int | None) -> None:
    assert len(events) == 1
    event = events[0]
    assert isinstance(event, TimelineSnapshot)
    assert len(event.items) == 1
    assert event.items[0]["item_id"] == item_id
    assert event.items[0]["kind"] == "agent_reasoning"
    assert event.items[0]["reasoning_ms"] == ms

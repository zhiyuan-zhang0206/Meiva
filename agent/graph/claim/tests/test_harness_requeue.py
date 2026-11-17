# meiva:note
# A refusal at the old turn's terminal boundary ends that delivery run. The inbox owns the retry,
# so the next run consumes the same row once without losing or duplicating its timeline entry.
# meiva:link agent/graph/docs/harness-foreground.md

from __future__ import annotations

import asyncio
from collections.abc import Sequence

import psycopg
from psycopg_pool import AsyncConnectionPool

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
from base.lm.harness import (
    BridgeEvent,
    HarnessInput,
    InputInContext,
    InputRejected,
    TurnOrigin,
    TurnStarted,
)


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

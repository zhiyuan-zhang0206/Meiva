# meiva:note
# The graph invocation of an external Codex agent whose timeline is projected from the thread's own record (the
# agent's `harness_transcript_timeline` setting on): the graph's branches are chosen by the presence of the writer, not
# by the harness, so Codex takes them with no code of its own, and this checks it. The CLI is scripted: handing it an
# input appends the items and rollout lines the real app-server writes to a fake item history and a rollout file and
# emits the events the adapter would; the Codex reader, the writer, the database and the invocation are real.
# A steer the interrupt swallowed is in no record: the stream reports it lost, the record cannot prove it, and it goes
# back to the queue and is sent again, once, whatever the stream says about the turn that lost it.
# meiva:link agent/graph/docs/harness-turn.md

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from itertools import count
from pathlib import Path

import psycopg
from langgraph.runtime import Runtime
from psycopg_pool import AsyncConnectionPool

from agent.graph.claim.harness_turn import harness_turn_node
from agent.graph.claim.tests.harness_support import (
    FakeEngine,
    config,
    in_a_turn,
    insert_row,
    row_status,
    runtime,
)
from agent.graph.claim.tests.harness_support import hosted_resources as hosted_resources
from agent.harness_timeline.plan import ReplyPolicy
from agent.harness_timeline.writer import HarnessTimelineWriter
from agent.ownership.tests.ownership_support import agent_row
from agent.state import AgentState
from base.agents.context import MeivaContext
from base.api_contracts.agent_contract import Harness
from base.clock.tests.fakes import system_clock
from base.lm.harness import (
    BridgeEvent,
    HarnessInput,
    InputLost,
)
from base.lm.harness.codex.transcript.shapes import Obj
from base.lm.harness.codex.transcript.tests.rig import STAMP, THREAD, Rig
from base.log.tests.log_records import LogRecords
from base.log.tests.log_records import loguru_records as loguru_records
from base.native_process.turn_identity import HostedTurnResources
from services.agent_runner.agent_host.transcript.effects import TimelineEffects

REPLY = "MEIVA_DISPLAY_TO_USER\nthe answer"


class _Published:
    def emit(self, payload: str) -> None:
        del payload


class ScriptedThread:
    def __init__(self, rig: Rig) -> None:
        self.rig = rig
        self.numbers = count()

    def write_input(self, key: str, text: str, turn: str) -> None:
        n = next(self.numbers)
        item: Obj = {
            "type": "userMessage",
            "id": f"u-{n}",
            "clientId": key,
            "content": [{"type": "text", "text": text}],
            "startedAtMs": 1,
            "completedAtMs": 1,
        }
        self.rig.feed([{"turnId": turn, "item": item, "startedAtMs": 1, "completedAtMs": 1}])

    def write_reply(self, text: str, turn: str) -> None:
        n = next(self.numbers)
        item: Obj = {
            "type": "agentMessage",
            "id": f"m-{n}",
            "text": text,
            "phase": "final_answer",
            "startedAtMs": 2,
            "completedAtMs": 2,
        }
        usage: Obj = {
            "type": "token_usage_record",
            "timestamp": STAMP,
            "ordinal": 9_000 + n,
            "payload": {
                "response_id": f"r-{n}",
                "turn_id": turn,
                "usage": {"input_tokens": 7, "output_tokens": 3},
            },
        }
        done: Obj = {
            "type": "event_msg",
            "timestamp": STAMP,
            "ordinal": 9_500 + n,
            "payload": {"type": "task_complete", "turn_id": turn},
        }
        self.rig.feed(
            [{"turnId": turn, "item": item, "startedAtMs": 2, "completedAtMs": 2}], [usage, done]
        )


def _wire(
    rt: Runtime[MeivaContext], pool: AsyncConnectionPool, agent_id: int, rig: Rig
) -> tuple[Runtime[MeivaContext], HarnessTimelineWriter]:
    effects = TimelineEffects(agent_id, _Published())
    ids = count()
    writer = HarnessTimelineWriter(
        pool=pool,
        agent_id=agent_id,
        harness="codex",
        session_id=THREAD,
        wire_id=rig.reader.wire_id,
        marker_id=lambda: str(next(ids)),
        incarnation=None,
        reader=rig.reader,
        policy=ReplyPolicy(explicit_visibility=False, delivery_mode=None),
        render_tool=lambda name, tool_input: f"{name}:{tool_input}",
        after_commit=effects.after_commit,
        cli_version=lambda: "0.166.0",
        clock=system_clock(),
    )
    wired = replace(rt.context, harness=Harness.CODEX, harness_timeline=writer)
    effects.bind(wired)
    return Runtime(context=wired), writer


async def _timeline(pool: AsyncConnectionPool, agent_id: int) -> list[tuple[str, str | None]]:
    async with pool.connection() as conn:
        cursor = await conn.execute(
            "SELECT item->>'kind', source_key FROM agent_harness_messages "
            "WHERE agent_id = %s ORDER BY seq",
            (agent_id,),
        )
        return [(str(kind), key) for kind, key in await cursor.fetchall()]


async def test_the_thread_writes_the_timeline_and_settles_the_input_by_its_client_id(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    hosted_resources: HostedTurnResources,
    loguru_records: LogRecords,
    tmp_path: Path,
) -> None:
    agent_id = agent_row(db_conn)
    row = insert_row(db_conn, agent_id, "chat", "user", "hello")
    rig = Rig(tmp_path)
    thread = ScriptedThread(rig)

    def script(inputs: Sequence[HarnessInput]) -> Sequence[BridgeEvent]:
        keys = [item.key for item in inputs]
        for key, item in zip(keys, inputs, strict=True):
            thread.write_input(key, item.text, "turn-1")
        thread.write_reply(REPLY, "turn-1")
        return in_a_turn(keys, REPLY)

    engine = FakeEngine(script=script)
    rt, writer = _wire(
        runtime(engine, ops_pool=aops_pool, hosted_resources=hosted_resources),
        aops_pool,
        agent_id,
        rig,
    )

    command = await harness_turn_node(AgentState(), rt, config(agent_id))

    assert command.goto == "claim"
    assert row_status(db_conn, row) == "done"
    timeline = await _timeline(aops_pool, agent_id)
    assert [kind for kind, _ in timeline] == ["inbound_chat", "agent_chat"]
    assert all(key is not None for _, key in timeline)
    assert writer.closed_turns == 1
    spend = loguru_records.events("llm_usage")
    assert [(r["extra"]["usage_kind"], r["extra"]["in_total"]) for r in spend] == [("agent", 7)]
    assert not loguru_records.events("harness_timeline_turn_end_unseen")
    assert not loguru_records.events("harness_timeline_usage_mismatch")


async def test_a_steer_the_interrupt_swallowed_is_not_in_the_thread_and_goes_back_to_the_queue(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    hosted_resources: HostedTurnResources,
    loguru_records: LogRecords,
    tmp_path: Path,
) -> None:
    agent_id = agent_row(db_conn)
    kept = insert_row(db_conn, agent_id, "chat", "user", "start the work")
    rig = Rig(tmp_path)
    thread = ScriptedThread(rig)
    lost = {"yet": False}
    turns = count(1)

    def script(inputs: Sequence[HarnessInput]) -> Sequence[BridgeEvent]:
        swallowed_now = [] if lost["yet"] else [i for i in inputs if "also do this" in i.text]
        lost["yet"] = lost["yet"] or bool(swallowed_now)
        taken = [item for item in inputs if item not in swallowed_now]
        events: list[BridgeEvent] = [InputLost(item.key, 1) for item in swallowed_now]
        if taken:
            turn = f"turn-{next(turns)}"
            for item in taken:
                thread.write_input(item.key, item.text, turn)
            thread.write_reply(REPLY, turn)
            events = [*in_a_turn([item.key for item in taken], REPLY, turn_id=turn), *events]
        return events

    engine = FakeEngine(script=script)
    rt, _ = _wire(
        runtime(engine, ops_pool=aops_pool, hosted_resources=hosted_resources),
        aops_pool,
        agent_id,
        rig,
    )

    _ = await harness_turn_node(AgentState(), rt, config(agent_id))
    assert row_status(db_conn, kept) == "done"
    swallowed = insert_row(db_conn, agent_id, "chat", "user", "also do this")

    _ = await harness_turn_node(AgentState(), rt, config(agent_id))

    assert row_status(db_conn, swallowed) == "done", (
        "sent again within the same invocation, and shown"
    )
    assert loguru_records.events("harness_input_requeued")
    timeline = await _timeline(aops_pool, agent_id)
    assert [kind for kind, _ in timeline] == [
        "inbound_chat",
        "agent_chat",
        "inbound_chat",
        "agent_chat",
    ]
    assert len({key for _, key in timeline}) == 4, "no item twice"
    texts = [item.text for batch in engine.delivered for item in batch]
    assert sum("also do this" in text for text in texts) == 2, "once lost, once again"

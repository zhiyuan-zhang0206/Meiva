# meiva:note
# The graph invocation of an external Claude agent whose timeline is projected from the CLI's own transcript (the
# agent's `harness_transcript_timeline` setting on): the invocation delivers the inbox and ends the turn, and the
# session-scoped writer writes what the record shows and settles what it proves. The CLI is scripted: handing it an
# input appends the rows the real CLI writes to a scripted record and emits the events its adapter would; the
# writer, the database and the invocation are real.
# meiva:link agent/graph/docs/harness-turn.md

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from itertools import count
from pathlib import Path

import psycopg
import pytest
from langgraph.runtime import Runtime
from psycopg_pool import AsyncConnectionPool

from agent.graph.claim.harness_inbox import INPUT_KEY_FIELD
from agent.graph.claim.harness_turn import harness_turn_node
from agent.graph.claim.tests.harness_support import (
    FakeEngine,
    completed,
    config,
    in_a_turn,
    input_key,
    insert_row,
    row_status,
    runtime,
)
from agent.graph.claim.tests.harness_support import hosted_resources as hosted_resources
from agent.graph.llm_errors import FatalLLMStreamError
from agent.harness_timeline.plan import ReplyPolicy
from agent.harness_timeline.writer import HarnessTimelineWriter
from agent.ownership.tests.ownership_support import agent_row
from agent.state import AgentState
from base.agents.context import MeivaContext
from base.clock.tests.fakes import system_clock
from base.lm.harness import (
    BridgeEvent,
    HarnessInput,
    InputRejected,
    SessionFailed,
    TurnOrigin,
    TurnStarted,
    Usage,
)
from base.lm.harness.claude.transcript.reader import ClaudeRecordReader
from base.lm.harness.claude.transcript.tests.support import now_ns
from base.lm.harness.session.transcript.entries import (
    AgentText,
    AnyEntry,
    HumanInput,
    Order,
    TokenUsage,
    TurnEnd,
)
from base.lm.harness.session.transcript.ports import EntryBatch, StoredRow
from base.log.tests.log_records import LogRecords
from base.log.tests.log_records import loguru_records as loguru_records
from base.native_process.turn_identity import HostedTurnResources
from services.agent_runner.agent_host.transcript.effects import TimelineEffects

REPLY = "MEIVA_DISPLAY_TO_USER\nthe answer"
key_uuid = ClaudeRecordReader(
    now_ns=now_ns, claude_home=Path("/nonexistent"), session_id="x"
).wire_id


# meiva:note
# The CLI's transcript as the writer's reader sees it: entries become visible as the CLI writes them.
@dataclass
class ScriptedRecord:
    entries: list[AnyEntry] = field(default_factory=list)
    visible: int = 0
    sequence: count[int] = field(default_factory=count)
    failing_reads: int = 0

    async def records(self, *, exhaustive: bool = False) -> tuple[str, ...]:
        return ("main",)

    def wire_id(self, key: str) -> str:
        return key_uuid(key)

    def parse_stored(self, rows: Sequence[StoredRow]) -> tuple[AnyEntry, ...]:
        return tuple(self.entries[row.ordinal] for row in rows)

    async def position_before(
        self, record: str, wire_ids: Sequence[str], *, after_ordinal: int = -1
    ) -> str | None:
        return None

    async def read_after(self, record: str, position: str | None) -> EntryBatch | None:
        if self.failing_reads:
            self.failing_reads -= 1
            raise OSError("the transcript could not be read")
        start = 0 if position is None else int(position)
        if self.visible <= start:
            return None
        chunk = tuple(self.entries[start : self.visible])
        stored = tuple(
            StoredRow(record, start + i, {"entry": start + i}) for i in range(len(chunk))
        )
        return EntryBatch(record, chunk, str(self.visible), stored)

    def write(self, *entries: AnyEntry) -> None:
        self.entries.extend(entries)
        self.visible = len(self.entries)

    def take(self, key: str, text: str, *, turn: str = "turn-1") -> HumanInput:
        n = next(self.sequence)
        return HumanInput(
            key=f"claude:u-{n}",
            order=Order(len(self.entries)),
            turn=turn,
            wire_id=key_uuid(key),
            text=text,
        )

    def reply(self, text: str, *, turn: str = "turn-1", close: bool = True) -> list[AnyEntry]:
        n = next(self.sequence)
        out: list[AnyEntry] = [
            AgentText(
                key=f"claude:a-{n}",
                order=Order(len(self.entries)),
                turn=turn,
                text=text,
                message_ref=f"msg-{n}",
            ),
            TokenUsage(
                key=f"claude:a-{n}:usage",
                order=Order(len(self.entries), 1),
                turn=turn,
                message_ref=f"msg-{n}",
                usage=Usage(input_tokens=7, output_tokens=3),
                model="claude-sonnet-5-5",
            ),
        ]
        if close:
            out.append(
                TurnEnd(key=f"claude:a-{n}:end", order=Order(len(self.entries), 2), turn=turn)
            )
        return out


class _Published:
    def emit(self, payload: str) -> None:
        del payload


def _wire(
    rt: Runtime[MeivaContext],
    pool: AsyncConnectionPool,
    agent_id: int,
    record: ScriptedRecord,
    *,
    reading: bool = False,
) -> tuple[Runtime[MeivaContext], HarnessTimelineWriter]:
    effects = TimelineEffects(agent_id, _Published())
    ids = count()
    writer = HarnessTimelineWriter(
        pool=pool,
        agent_id=agent_id,
        harness="claude_code",
        session_id="session-1",
        wire_id=key_uuid,
        marker_id=lambda: str(next(ids)),
        incarnation=None,
        reader=record,
        policy=ReplyPolicy(explicit_visibility=False, delivery_mode=None),
        render_tool=lambda name, tool_input: f"{name}:{tool_input}",
        after_commit=effects.after_commit,
        cli_version=lambda: None,
        clock=system_clock(),
    )
    if reading:
        writer.start_reading()
    wired = replace(rt.context, harness_timeline=writer)
    effects.bind(wired)
    return Runtime(context=wired), writer


def _cli(record: ScriptedRecord, *, reply: str = REPLY, close: bool = True):
    def script(inputs: Sequence[HarnessInput]) -> Sequence[BridgeEvent]:
        keys = [item.key for item in inputs]
        record.write(*(record.take(key, item.text) for key, item in zip(keys, inputs, strict=True)))
        record.write(*record.reply(reply, close=close))
        return in_a_turn(keys, reply)

    return script


async def _timeline(pool: AsyncConnectionPool, agent_id: int) -> list[tuple[str, str | None]]:
    async with pool.connection() as conn:
        cursor = await conn.execute(
            "SELECT item->>'kind', source_key FROM agent_harness_messages "
            "WHERE agent_id = %s ORDER BY seq",
            (agent_id,),
        )
        return [(str(kind), key) for kind, key in await cursor.fetchall()]


async def test_the_record_writes_the_timeline_and_settles_the_input_and_the_events_write_nothing(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    hosted_resources: HostedTurnResources,
    loguru_records: LogRecords,
) -> None:
    agent_id = agent_row(db_conn)
    row = insert_row(db_conn, agent_id, "chat", "user", "hello")
    record = ScriptedRecord()
    engine = FakeEngine(script=_cli(record))
    rt, writer = _wire(
        runtime(engine, ops_pool=aops_pool, hosted_resources=hosted_resources),
        aops_pool,
        agent_id,
        record,
    )

    command = await harness_turn_node(AgentState(), rt, config(agent_id))

    assert command.goto == "claim"
    assert row_status(db_conn, row) == "done"
    timeline = await _timeline(aops_pool, agent_id)
    assert [kind for kind, _ in timeline] == ["inbound_chat", "agent_chat"]
    assert all(key is not None for _, key in timeline), "every item came from a keyed entry"
    assert writer.closed_turns == 1
    spend = loguru_records.events("llm_usage")
    assert [(r["extra"]["usage_kind"], r["extra"]["in_total"]) for r in spend] == [("agent", 7)]


async def test_a_turn_whose_closing_row_is_late_is_waited_for_and_one_that_never_comes_is_no_fault(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    hosted_resources: HostedTurnResources,
    loguru_records: LogRecords,
) -> None:
    agent_id = agent_row(db_conn)
    row = insert_row(db_conn, agent_id, "chat", "user", "hello")
    record = ScriptedRecord()
    engine = FakeEngine(script=_cli(record, close=False))
    rt, writer = _wire(
        runtime(engine, ops_pool=aops_pool, hosted_resources=hosted_resources),
        aops_pool,
        agent_id,
        record,
    )

    command = await harness_turn_node(AgentState(), rt, config(agent_id))

    assert command.goto == "claim" and row_status(db_conn, row) == "done"
    assert writer.closed_turns == 0
    unseen = loguru_records.events("harness_timeline_turn_end_unseen")
    assert [r["extra"]["outcome"] for r in unseen] == ["quiet"]
    assert [kind for kind, _ in await _timeline(aops_pool, agent_id)] == [
        "inbound_chat",
        "agent_chat",
    ]


async def test_a_claimed_row_the_record_shows_is_settled_by_the_writer_and_one_it_lacks_is_delivered_again(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    hosted_resources: HostedTurnResources,
) -> None:
    agent_id = agent_row(db_conn)
    shown = insert_row(db_conn, agent_id, "chat", "user", "was taken")
    lost = insert_row(db_conn, agent_id, "chat", "user", "was lost")
    for row in (shown, lost):
        db_conn.execute(
            "UPDATE inbound_messages SET status='claimed', claimed_at=now(), "
            "payload = COALESCE(payload, '{}'::jsonb) || jsonb_build_object(%s::text, %s::text) "
            "WHERE id=%s",
            (INPUT_KEY_FIELD, input_key(row), row),
        )
    db_conn.commit()
    record = ScriptedRecord()
    record.write(record.take(input_key(shown), "was taken"), *record.reply("ok"))
    engine = FakeEngine(script=_cli(record))
    rt, _ = _wire(
        runtime(engine, ops_pool=aops_pool, hosted_resources=hosted_resources),
        aops_pool,
        agent_id,
        record,
    )

    _ = await harness_turn_node(AgentState(), rt, config(agent_id))

    assert row_status(db_conn, shown) == "done" and row_status(db_conn, lost) == "done"
    delivered = [item.key for batch in engine.delivered for item in batch]
    assert input_key(shown) not in delivered, "an input the record shows is never sent twice"
    assert any(key == input_key(lost) or key.startswith("inbound-") for key in delivered)
    texts = [item.text for batch in engine.delivered for item in batch]
    assert any("was lost" in text for text in texts)
    assert any("restarted" in text for text in texts), "the recovery note rides the next input"


async def test_a_failure_returns_the_input_the_record_does_not_show_to_the_queue(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    hosted_resources: HostedTurnResources,
) -> None:
    agent_id = agent_row(db_conn)
    row = insert_row(db_conn, agent_id, "chat", "user", "hello")
    record = ScriptedRecord()

    def script(inputs: Sequence[HarnessInput]) -> Sequence[BridgeEvent]:
        keys = tuple(item.key for item in inputs)
        return [TurnStarted("t1", TurnOrigin.INPUT, keys, 1), SessionFailed(error="the CLI died")]

    engine = FakeEngine(script=script)
    rt, _ = _wire(
        runtime(engine, ops_pool=aops_pool, hosted_resources=hosted_resources),
        aops_pool,
        agent_id,
        record,
    )

    with pytest.raises(FatalLLMStreamError):
        await harness_turn_node(AgentState(), rt, config(agent_id))

    assert row_status(db_conn, row) == "pending"
    assert engine.retired


async def test_a_stop_writes_no_note_of_its_own_on_the_timeline(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    hosted_resources: HostedTurnResources,
) -> None:
    agent_id = agent_row(db_conn)
    _ = insert_row(db_conn, agent_id, "chat", "user", "hello")
    insert_row(
        db_conn,
        agent_id,
        "system_note",
        "system",
        "stopped by the user",
        payload='{"note_tag": "lifecycle_interrupt", "passive": true}',
    )
    record = ScriptedRecord()
    engine = FakeEngine(script=_cli(record))
    rt, _ = _wire(
        runtime(engine, ops_pool=aops_pool, hosted_resources=hosted_resources),
        aops_pool,
        agent_id,
        record,
    )

    _ = await harness_turn_node(AgentState(), rt, config(agent_id))

    async with aops_pool.connection() as conn:
        cursor = await conn.execute(
            "SELECT count(*) FROM agent_harness_messages WHERE agent_id=%s "
            "AND item->>'payload' LIKE %s",
            (agent_id, "%stopped by the user%"),
        )
        shown = await cursor.fetchone()
    assert shown == (0,), "the note rides into the CLI with the input, not onto the timeline"
    assert any("stopped by the user" in item.text for batch in engine.delivered for item in batch)


async def test_an_input_the_stream_calls_refused_but_the_record_shows_is_settled_not_sent_again(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    hosted_resources: HostedTurnResources,
    loguru_records: LogRecords,
) -> None:
    agent_id = agent_row(db_conn)
    row = insert_row(db_conn, agent_id, "chat", "user", "hello")
    record = ScriptedRecord()

    def script(inputs: Sequence[HarnessInput]) -> Sequence[BridgeEvent]:
        keys = tuple(item.key for item in inputs)
        record.write(*(record.take(key, item.text) for key, item in zip(keys, inputs, strict=True)))
        record.write(*record.reply(REPLY))
        return [
            TurnStarted("t1", TurnOrigin.INPUT, keys, 1),
            *(InputRejected(key, "the CLI said its queue was full", 1) for key in keys),
            completed("t1", REPLY),
        ]

    engine = FakeEngine(script=script)
    rt, _ = _wire(
        runtime(engine, ops_pool=aops_pool, hosted_resources=hosted_resources),
        aops_pool,
        agent_id,
        record,
    )

    _ = await harness_turn_node(AgentState(), rt, config(agent_id))

    assert row_status(db_conn, row) == "done", "the record proved delivery"
    assert len(engine.delivered) == 1, "the input reached the CLI once, whatever the event said"
    assert len(loguru_records.events("harness_input_proven_by_record")) == 1
    assert loguru_records.events("harness_input_requeued") == []

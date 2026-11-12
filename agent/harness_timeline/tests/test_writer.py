from __future__ import annotations

import json
import shutil
from collections.abc import Mapping, Sequence
from pathlib import Path
from uuid import uuid4

import pytest
from psycopg_pool import AsyncConnectionPool

from agent.harness_timeline.plan import ReplyPolicy
from agent.harness_timeline.writer import Committed, HarnessTimelineWriter, TurnEndWait
from agent.ownership.inbound import RuntimeOwnershipLostError
from base.agents.history.timeline_item import TimelineItem
from base.clock.tests.fakes import system_clock
from base.lm.harness.claude.transcript.reader import ClaudeRecordReader
from base.lm.harness.claude.transcript.tests.support import FIXTURES, entries_of, rows
from base.lm.harness.session.transcript.drafts import InboundPart, SentInput
from base.lm.harness.session.transcript.entries import AnyEntry
from base.lm.harness.session.transcript.ports import EntryBatch, RecordReader, StoredRow
from base.native_process.runtime_incarnation import RuntimeIncarnation

BASIC = "3c3d2de5-c479-52b7-91a6-fce4e4d26db9"
SUBS = "611dfcac-5809-5de8-b8c6-2e185bf7d5be"
POLICY = ReplyPolicy(explicit_visibility=False, delivery_mode=None)


def _render(name: str, tool_input: Mapping[str, object]) -> str:
    return f"{name}:{json.dumps(dict(tool_input), sort_keys=True)}"


class _ScriptedReader:
    def __init__(self, records: dict[str, list[AnyEntry]]) -> None:
        self.records_data = records
        self.visible = dict.fromkeys(records, 0)
        self.fail_next = False

    async def records(self) -> tuple[str, ...]:
        return tuple(self.records_data)

    def parse_stored(self, rows: Sequence[StoredRow]) -> tuple[AnyEntry, ...]:
        return tuple(self.records_data[rows[0].record][row.ordinal] for row in rows) if rows else ()

    async def read_after(self, record: str, position: str | None) -> EntryBatch | None:
        if self.fail_next:
            self.fail_next = False
            raise OSError("the transcript could not be read")
        start = 0 if position is None else int(position)
        end = self.visible[record]
        if end <= start:
            return None
        entries = tuple(self.records_data[record][start:end])
        stored = tuple(
            StoredRow(record, start + i, {"entry": start + i}) for i in range(len(entries))
        )
        return EntryBatch(record, entries, str(end), stored)


async def _agent(
    pool: AsyncConnectionPool, *, owner: bool = False
) -> tuple[int, RuntimeIncarnation | None]:
    async with pool.connection() as conn:
        cursor = await conn.execute("INSERT INTO agents DEFAULT VALUES RETURNING id")
        row = await cursor.fetchone()
        assert row is not None
        agent_id = int(row[0])
        generation, owner_id = uuid4(), uuid4()
        await conn.execute(
            "INSERT INTO agents_meta (id, status, harness, runtime_generation, runtime_owner) "
            "VALUES (%s, 'idling', 'claude_code', %s, %s)",
            (agent_id, generation, owner_id),
        )
        return agent_id, RuntimeIncarnation(agent_id, generation, owner_id)


async def _inbound_row(pool: AsyncConnectionPool, agent_id: int, status: str = "claimed") -> int:
    async with pool.connection() as conn:
        cursor = await conn.execute(
            "INSERT INTO inbound_messages (agent_id, content, kind, source, status) "
            "VALUES (%s, 'hello', 'chat', 'web', %s) RETURNING id",
            (agent_id, status),
        )
        row = await cursor.fetchone()
        assert row is not None
        return int(row[0])


async def _timeline(pool: AsyncConnectionPool, agent_id: int) -> list[tuple[str, str, str | None]]:
    async with pool.connection() as conn:
        cursor = await conn.execute(
            "SELECT item->>'kind', item->>'item_id', item->>'parent_item_id' "
            "FROM agent_harness_messages WHERE agent_id = %s ORDER BY seq",
            (agent_id,),
        )
        return [
            (str(a), str(b), None if c is None else str(c)) for a, b, c in await cursor.fetchall()
        ]


async def _status(pool: AsyncConnectionPool, row_id: int) -> str:
    async with pool.connection() as conn:
        cursor = await conn.execute("SELECT status FROM inbound_messages WHERE id = %s", (row_id,))
        row = await cursor.fetchone()
        assert row is not None
        return str(row[0])


def _writer(
    pool: AsyncConnectionPool,
    agent_id: int,
    incarnation: RuntimeIncarnation | None,
    reader: RecordReader,
    committed: list[Committed],
) -> HarnessTimelineWriter:
    async def after(commit: Committed) -> None:
        committed.append(commit)

    return HarnessTimelineWriter(
        pool=pool,
        agent_id=agent_id,
        harness="claude_code",
        session_id="session-1",
        incarnation=incarnation,
        reader=reader,
        policy=POLICY,
        render_tool=_render,
        after_commit=after,
        cli_version=lambda: "2.1.322",
        clock=system_clock(),
    )


def _send(writer: HarnessTimelineWriter, wire: str, key: str, row_id: int) -> None:
    item = TimelineItem(item_id="x", kind="inbound_chat", payload="hello", inbound_id=row_id)
    writer.register_sent(
        SentInput(
            key=key,
            wire_id=wire,
            text="hello",
            parts=(InboundPart(ref=f"row:{row_id}", text="hello"),),
        ),
        items={f"row:{row_id}": item},
        inbound_ids=(row_id,),
    )


async def test_a_turn_is_written_once_in_read_order_and_settles_its_input(
    aops_pool: AsyncConnectionPool,
) -> None:
    agent_id, incarnation = await _agent(aops_pool)
    row = await _inbound_row(aops_pool, agent_id)
    reader = _ScriptedReader({"main": entries_of("basic")})
    committed: list[Committed] = []
    writer = _writer(aops_pool, agent_id, incarnation, reader, committed)
    _send(writer, BASIC, "k1", row)
    reader.visible["main"] = 5
    assert await writer.read_now()
    reader.visible["main"] = len(reader.records_data["main"])
    assert await writer.read_now()
    assert not await writer.read_now()
    kinds = [kind for kind, _, _ in await _timeline(aops_pool, agent_id)]
    assert kinds == [
        "inbound_chat",
        "agent_reasoning",
        "agent_code",
        "code_output",
        "agent_reasoning",
        "agent_chat",
    ]
    assert await _status(aops_pool, row) == "done"
    assert sum(len(c.written) for c in committed) == 6
    assert writer.closed_turns == 1
    assert [(s.input_key) for c in committed for s in c.plan.settlements] == ["k1"]


async def test_a_requeued_row_proven_by_the_record_is_settled_not_delivered_again(
    aops_pool: AsyncConnectionPool,
) -> None:
    agent_id, incarnation = await _agent(aops_pool)
    row = await _inbound_row(aops_pool, agent_id, status="pending")
    reader = _ScriptedReader({"main": entries_of("basic")})
    reader.visible["main"] = 6
    writer = _writer(aops_pool, agent_id, incarnation, reader, [])
    _send(writer, BASIC, "k1", row)
    assert await writer.read_now()
    assert await _status(aops_pool, row) == "done"


async def test_a_restart_replays_silently_then_continues_after_the_stored_position(
    aops_pool: AsyncConnectionPool,
) -> None:
    agent_id, incarnation = await _agent(aops_pool)
    row = await _inbound_row(aops_pool, agent_id)
    reader = _ScriptedReader({"main": entries_of("basic")})
    reader.visible["main"] = 6
    first = _writer(aops_pool, agent_id, incarnation, reader, [])
    _send(first, BASIC, "k1", row)
    assert await first.read_now()
    before = await _timeline(aops_pool, agent_id)
    committed: list[Committed] = []
    second = _writer(aops_pool, agent_id, incarnation, reader, committed)
    _send(second, BASIC, "k1", row)
    assert not await second.read_now()
    assert await _timeline(aops_pool, agent_id) == before
    reader.visible["main"] = len(reader.records_data["main"])
    assert await second.read_now()
    after = await _timeline(aops_pool, agent_id)
    assert after[: len(before)] == before and len(after) == 6
    assert len({item_id for _, item_id, _ in after}) == len(after)


async def test_a_failed_read_changes_nothing_and_the_next_read_proceeds(
    aops_pool: AsyncConnectionPool,
) -> None:
    agent_id, incarnation = await _agent(aops_pool)
    row = await _inbound_row(aops_pool, agent_id)
    reader = _ScriptedReader({"main": entries_of("basic")})
    reader.visible["main"] = 6
    writer = _writer(aops_pool, agent_id, incarnation, reader, [])
    _send(writer, BASIC, "k1", row)
    assert await writer.read_now() is True
    reader.fail_next = True
    with pytest.raises(OSError, match="could not be read"):
        _ = await writer.read_now()
    assert len(await _timeline(aops_pool, agent_id)) == 4
    assert not await writer.read_now()


async def test_a_stale_incarnation_writes_nothing(aops_pool: AsyncConnectionPool) -> None:
    agent_id, _ = await _agent(aops_pool)
    row = await _inbound_row(aops_pool, agent_id)
    stale = RuntimeIncarnation(agent_id, uuid4(), uuid4())
    reader = _ScriptedReader({"main": entries_of("basic")})
    reader.visible["main"] = 6
    writer = _writer(aops_pool, agent_id, stale, reader, [])
    _send(writer, BASIC, "k1", row)
    with pytest.raises(RuntimeOwnershipLostError):
        _ = await writer.read_now()
    assert await _timeline(aops_pool, agent_id) == []
    assert await _status(aops_pool, row) == "claimed"


async def test_sub_agent_rows_nest_under_the_call_item_even_when_read_in_a_later_pass(
    aops_pool: AsyncConnectionPool,
) -> None:
    agent_id, incarnation = await _agent(aops_pool)
    row = await _inbound_row(aops_pool, agent_id)
    main = entries_of("subagents")
    sync_sub = entries_of("subagents", "af6798fb938fb227f")
    async_sub = entries_of("subagents", "a0044dfe6d0ac4533")
    reader = _ScriptedReader(
        {"main": main, "af6798fb938fb227f": sync_sub, "a0044dfe6d0ac4533": async_sub}
    )
    reader.visible.update(
        {"main": len(main), "af6798fb938fb227f": len(sync_sub), "a0044dfe6d0ac4533": 0}
    )
    writer = _writer(aops_pool, agent_id, incarnation, reader, [])
    _send(writer, SUBS, "k", row)
    assert await writer.read_now()
    reader.visible["a0044dfe6d0ac4533"] = len(async_sub)
    assert await writer.read_now()
    timeline = await _timeline(aops_pool, agent_id)
    calls = {item_id for kind, item_id, _ in timeline if kind == "agent_code"}
    nested = [(kind, parent) for kind, _, parent in timeline if parent is not None]
    assert len(nested) == 6 and all(parent in calls for _, parent in nested)
    assert len({parent for _, parent in nested}) == 2


async def test_the_turn_end_wait_sees_a_closing_entry_or_gives_up_quietly(
    aops_pool: AsyncConnectionPool,
) -> None:
    agent_id, incarnation = await _agent(aops_pool)
    reader = _ScriptedReader({"main": entries_of("basic")})
    reader.visible["main"] = len(reader.records_data["main"])
    writer = _writer(aops_pool, agent_id, incarnation, reader, [])
    closed = await writer.wait_for_turn_end(0, quiet_seconds=0.05, ceiling_seconds=2.0)
    assert closed is TurnEndWait.CLOSED
    quiet = await writer.wait_for_turn_end(
        writer.closed_turns, quiet_seconds=0.05, ceiling_seconds=2.0
    )
    assert quiet is TurnEndWait.QUIET
    ceiling = await writer.wait_for_turn_end(
        writer.closed_turns, quiet_seconds=5.0, ceiling_seconds=0.05
    )
    assert ceiling is TurnEndWait.CEILING


async def test_the_writer_reads_a_real_transcript_file_by_position_end_to_end(
    aops_pool: AsyncConnectionPool, tmp_path: Path
) -> None:
    session = rows("basic")[0]["sessionId"]
    assert isinstance(session, str)
    folder = tmp_path / "projects" / "p"
    folder.mkdir(parents=True)
    target = folder / f"{session}.jsonl"
    lines = (FIXTURES / "basic" / "main.jsonl").read_text().splitlines(keepends=True)
    target.write_text("".join(lines[:9]))
    agent_id, incarnation = await _agent(aops_pool)
    row = await _inbound_row(aops_pool, agent_id)
    reader = ClaudeRecordReader(claude_home=tmp_path, session_id=session)
    writer = _writer(aops_pool, agent_id, incarnation, reader, [])
    _send(writer, BASIC, "k1", row)
    _ = await writer.read_now()
    _ = shutil.copyfile(FIXTURES / "basic" / "main.jsonl", target)
    _ = await writer.read_now()
    restarted = _writer(
        aops_pool,
        agent_id,
        incarnation,
        ClaudeRecordReader(claude_home=tmp_path, session_id=session),
        [],
    )
    _send(restarted, BASIC, "k1", row)
    assert not await restarted.read_now()
    kinds = [kind for kind, _, _ in await _timeline(aops_pool, agent_id)]
    assert kinds.count("inbound_chat") == 1 and kinds[-1] == "agent_chat" and len(kinds) == 6


async def _rebuild_ordinal(pool: AsyncConnectionPool, agent_id: int) -> int:
    async with pool.connection() as conn:
        cursor = await conn.execute(
            "SELECT rebuild_ordinal FROM agent_harness_transcript_positions "
            "WHERE agent_id = %s AND record = 'main'",
            (agent_id,),
        )
        row = await cursor.fetchone()
        assert row is not None
        return int(row[0])


async def test_a_restart_rebuilds_from_the_stored_rows_and_pairs_a_result_with_its_earlier_call(
    aops_pool: AsyncConnectionPool,
) -> None:
    agent_id, incarnation = await _agent(aops_pool)
    row = await _inbound_row(aops_pool, agent_id)
    entries = entries_of("basic")
    result_at = next(i for i, entry in enumerate(entries) if type(entry).__name__ == "ToolResult")
    reader = _ScriptedReader({"main": entries})
    reader.visible["main"] = result_at
    first = _writer(aops_pool, agent_id, incarnation, reader, [])
    _send(first, BASIC, "k1", row)
    assert await first.read_now()
    assert await _rebuild_ordinal(aops_pool, agent_id) == -1
    kinds_before = [kind for kind, _, _ in await _timeline(aops_pool, agent_id)]
    assert kinds_before[-1] == "agent_code"
    reader.visible["main"] = len(entries)
    second = _writer(aops_pool, agent_id, incarnation, reader, [])
    _send(second, BASIC, "k1", row)
    assert await second.read_now()
    kinds = [kind for kind, _, _ in await _timeline(aops_pool, agent_id)]
    assert kinds == [
        "inbound_chat",
        "agent_reasoning",
        "agent_code",
        "code_output",
        "agent_reasoning",
        "agent_chat",
    ]
    assert await _rebuild_ordinal(aops_pool, agent_id) == len(entries) - 1


async def test_the_rebuild_point_stays_put_while_a_call_is_open_and_moves_when_the_turn_is_quiet(
    aops_pool: AsyncConnectionPool,
) -> None:
    agent_id, incarnation = await _agent(aops_pool)
    row = await _inbound_row(aops_pool, agent_id)
    entries = entries_of("basic")
    reader = _ScriptedReader({"main": entries})
    writer = _writer(aops_pool, agent_id, incarnation, reader, [])
    _send(writer, BASIC, "k1", row)
    for visible in (3, len(entries)):
        reader.visible["main"] = visible
        assert await writer.read_now()
    assert await _rebuild_ordinal(aops_pool, agent_id) == len(entries) - 1
    async with aops_pool.connection() as conn:
        cursor = await conn.execute(
            "SELECT count(*) FROM agent_harness_transcript_rows WHERE agent_id = %s", (agent_id,)
        )
        stored = await cursor.fetchone()
    assert stored == (len(entries),)

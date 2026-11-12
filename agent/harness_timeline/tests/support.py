# meiva:note
# The writer tests' shared helpers: a scripted reader (entries become visible as a test says), a registered agent with a
# runtime row, inbound rows, timeline and status readers, and a writer wired over them.
# meiva:link agent/harness_timeline/README.md

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from itertools import count
from uuid import uuid4

from psycopg_pool import AsyncConnectionPool

from agent.harness_timeline.plan import ReplyPolicy
from agent.harness_timeline.writer import Committed, HarnessTimelineWriter
from base.agents.history.timeline_item import TimelineItem
from base.clock.tests.fakes import system_clock
from base.lm.harness.session.transcript.drafts import InboundPart, SentInput
from base.lm.harness.session.transcript.entries import AnyEntry, HumanInput
from base.lm.harness.session.transcript.ports import EntryBatch, RecordReader, StoredRow
from base.native_process.runtime_incarnation import RuntimeIncarnation

BASIC = "3c3d2de5-c479-52b7-91a6-fce4e4d26db9"
SUBS = "611dfcac-5809-5de8-b8c6-2e185bf7d5be"
POLICY = ReplyPolicy(explicit_visibility=False, delivery_mode=None)


def render_tool(name: str, tool_input: Mapping[str, object]) -> str:
    return f"{name}:{json.dumps(dict(tool_input), sort_keys=True)}"


class ScriptedReader:
    def __init__(self, records: dict[str, list[AnyEntry]]) -> None:
        self.records_data = records
        self.visible = dict.fromkeys(records, 0)
        self.fail_next = False

    async def records(self) -> tuple[str, ...]:
        return tuple(self.records_data)

    def parse_stored(self, rows: Sequence[StoredRow]) -> tuple[AnyEntry, ...]:
        return tuple(self.records_data[rows[0].record][row.ordinal] for row in rows) if rows else ()

    def wire_id(self, key: str) -> str:
        return key

    async def position_before(self, record: str, wire_ids: Sequence[str]) -> str | None:
        end = self.visible[record]
        for index, entry in enumerate(self.records_data[record][:end]):
            if isinstance(entry, HumanInput) and entry.wire_id in wire_ids:
                return str(index) if index else None
        return str(end) if end else None

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


async def make_agent(
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


async def make_inbound_row(
    pool: AsyncConnectionPool, agent_id: int, status: str = "claimed"
) -> int:
    async with pool.connection() as conn:
        cursor = await conn.execute(
            "INSERT INTO inbound_messages (agent_id, content, kind, source, status) "
            "VALUES (%s, 'hello', 'chat', 'web', %s) RETURNING id",
            (agent_id, status),
        )
        row = await cursor.fetchone()
        assert row is not None
        return int(row[0])


async def timeline_of(
    pool: AsyncConnectionPool, agent_id: int
) -> list[tuple[str, str, str | None]]:
    async with pool.connection() as conn:
        cursor = await conn.execute(
            "SELECT item->>'kind', item->>'item_id', item->>'parent_item_id' "
            "FROM agent_harness_messages WHERE agent_id = %s ORDER BY seq",
            (agent_id,),
        )
        return [
            (str(a), str(b), None if c is None else str(c)) for a, b, c in await cursor.fetchall()
        ]


async def status_of(pool: AsyncConnectionPool, row_id: int) -> str:
    async with pool.connection() as conn:
        cursor = await conn.execute("SELECT status FROM inbound_messages WHERE id = %s", (row_id,))
        row = await cursor.fetchone()
        assert row is not None
        return str(row[0])


def make_writer(
    pool: AsyncConnectionPool,
    agent_id: int,
    incarnation: RuntimeIncarnation | None,
    reader: RecordReader,
    committed: list[Committed],
    *,
    session: str = "session-1",
    reading: bool = True,
) -> HarnessTimelineWriter:
    async def after(commit: Committed) -> None:
        committed.append(commit)

    markers = count()
    writer = HarnessTimelineWriter(
        pool=pool,
        agent_id=agent_id,
        harness="claude_code",
        session_id=session,
        marker_id=lambda: str(next(markers)),
        incarnation=incarnation,
        reader=reader,
        policy=POLICY,
        render_tool=render_tool,
        after_commit=after,
        cli_version=lambda: "2.1.322",
        clock=system_clock(),
    )
    if reading:
        writer.start_reading()
    return writer


def send_input(writer: HarnessTimelineWriter, wire: str, key: str, row_id: int) -> None:
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

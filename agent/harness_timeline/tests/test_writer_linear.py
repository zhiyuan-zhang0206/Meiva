# meiva:note
# The writer's whole chain is linear in the length of the conversation: a long session (thousands of rows) is fed
# to the real reader and writer in batches, and every statement the writer sends is counted with the rows it read
# or wrote (a cursor class that records each statement and its row count). The rows read and written per batch do
# not grow with what is already stored, the stored rows are the whitelisted ones only, and a restart reads back only
# the rows after the last quiet point, never the session.
# meiva:link agent/harness_timeline/README.md

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Self
from uuid import uuid4

import psycopg
from psycopg import AsyncCursor
from psycopg_pool import AsyncConnectionPool

from agent.harness_timeline.plan import ReplyPolicy
from agent.harness_timeline.writer import Committed, HarnessTimelineWriter
from base.clock.tests.fakes import system_clock
from base.lm.harness.claude.transcript.reader import ClaudeRecordReader
from base.lm.harness.claude.transcript.tests.support import FIXTURES, rows
from base.native_process.runtime_incarnation import RuntimeIncarnation

TURNS = 400
BATCH_LINES = 180


class StatementLog:
    def __init__(self) -> None:
        self.statements: list[tuple[str, int]] = []

    def drain(self) -> list[tuple[str, int]]:
        taken, self.statements = self.statements, []
        return taken


LOG = StatementLog()


class CountingCursor(AsyncCursor[tuple[object, ...]]):
    async def execute(self, *args: Any, **kwargs: Any) -> Self:
        result = await super().execute(*args, **kwargs)
        self._record(args[0])
        return result

    async def executemany(self, *args: Any, **kwargs: Any) -> None:
        await super().executemany(*args, **kwargs)
        self._record(args[0])

    def _record(self, query: object) -> None:
        text = query if isinstance(query, str) else str(query)
        LOG.statements.append((text.lstrip().split(None, 1)[0].upper(), max(self.rowcount, 0)))


def _session_lines() -> list[str]:
    lines = (FIXTURES / "basic" / "main.jsonl").read_text().splitlines()
    out: list[str] = []
    for turn in range(TURNS):
        for line in lines:
            row = json.loads(line)
            if "uuid" in row:
                row["uuid"] = f"{row['uuid']}-{turn}"
            if isinstance(row.get("promptId"), str):
                row["promptId"] = f"{row['promptId']}-{turn}"
            message = row.get("message")
            if isinstance(message, dict) and "id" in message:
                message["id"] = f"{message['id']}-{turn}"
            out.append(json.dumps(row))
            out.append(
                json.dumps({"type": "api-request-blob", "uuid": f"b-{turn}", "x": "r" * 300})
            )
    return out


async def _scalar(cursor: AsyncCursor[tuple[object, ...]]) -> int:
    row = await cursor.fetchone()
    assert row is not None and isinstance(row[0], int)
    return row[0]


async def _noop(commit: Committed) -> None:
    del commit


def _writer(
    pool: AsyncConnectionPool[Any],
    agent_id: int,
    incarnation: RuntimeIncarnation,
    home: Path,
    sid: str,
) -> HarnessTimelineWriter:
    writer = HarnessTimelineWriter(
        pool=pool,
        agent_id=agent_id,
        harness="claude_code",
        session_id=sid,
        marker_id=lambda: "m",
        incarnation=incarnation,
        reader=ClaudeRecordReader(claude_home=home, session_id=sid),
        policy=ReplyPolicy(explicit_visibility=False, delivery_mode=None),
        render_tool=lambda name, tool_input: f"{name}{sorted(tool_input)}",
        after_commit=_noop,
        cli_version=lambda: None,
        clock=system_clock(),
    )
    writer.start_reading()
    return writer


def _volume(statements: list[tuple[str, int]]) -> tuple[int, int, int]:
    read = sum(rows_ for kind, rows_ in statements if kind == "SELECT")
    written = sum(rows_ for kind, rows_ in statements if kind in ("INSERT", "UPDATE"))
    return read, written, len(statements)


async def test_a_long_session_costs_constant_database_work_per_batch_and_a_restart_reads_only_its_tail(
    db_url: str, tmp_path: Path
) -> None:
    session = rows("basic")[0]["sessionId"]
    assert isinstance(session, str)
    target = tmp_path / "projects" / "p" / f"{session}.jsonl"
    target.parent.mkdir(parents=True)
    target.write_text("")
    lines = _session_lines()
    pool = AsyncConnectionPool(
        db_url,
        min_size=1,
        max_size=2,
        kwargs={"autocommit": True, "prepare_threshold": None, "cursor_factory": CountingCursor},
        open=False,
    )
    await pool.open()
    try:
        async with pool.connection() as conn:
            cursor = await conn.execute("INSERT INTO agents DEFAULT VALUES RETURNING id")
            agent_id = await _scalar(cursor)
            generation, owner = uuid4(), uuid4()
            await conn.execute(
                "INSERT INTO agents_meta (id, status, harness, runtime_generation, runtime_owner) "
                "VALUES (%s, 'idling', 'claude_code', %s, %s)",
                (agent_id, generation, owner),
            )
        incarnation = RuntimeIncarnation(agent_id, generation, owner)
        writer = _writer(pool, agent_id, incarnation, tmp_path, session)
        per_batch: list[tuple[int, int, int]] = []
        for start in range(0, len(lines), BATCH_LINES):
            with target.open("a") as handle:
                handle.write("\n".join(lines[start : start + BATCH_LINES]) + "\n")
            _ = LOG.drain()
            while await writer.read_now():
                pass
            per_batch.append(_volume(LOG.drain()))
        full = per_batch[:-1]
        quarter = max(len(full) // 4, 2)
        early, late = full[1 : quarter + 1], full[-quarter:]
        for column in range(3):
            assert (
                max(batch[column] for batch in late)
                <= 1.5 * max(batch[column] for batch in early) + 30
            ), per_batch
        async with pool.connection() as conn:
            cursor = await conn.execute(
                "SELECT count(*) FROM agent_harness_transcript_rows WHERE agent_id = %s "
                "AND row->>'type' LIKE 'api-request%%'",
                (agent_id,),
            )
            assert await cursor.fetchone() == (0,)
            cursor = await conn.execute(
                "SELECT count(*) FROM agent_harness_transcript_rows WHERE agent_id = %s",
                (agent_id,),
            )
            stored = await _scalar(cursor)
        assert stored > 2_000
        _ = LOG.drain()
        restarted = _writer(pool, agent_id, incarnation, tmp_path, session)
        assert not await restarted.read_now()
        read, written, _ = _volume(LOG.drain())
        assert written <= 5 and read < stored // 10, (read, written, stored)
    finally:
        await pool.close()
    _ = (psycopg, uuid4)

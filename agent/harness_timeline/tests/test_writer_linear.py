# meiva:note
# The writer's whole chain is linear in the length of the conversation, measured where a quadratic term would hide:
# in what the database reads, not in the rows it returns. A long session (a main record of hundreds of turns, forty
# background sub-agents that keep producing, calls that are never answered) is fed to the real reader and writer in
# rounds, and for every statement the writer sends, inside a transaction or outside one, what the database read for it
# is taken from its own counters (the tuples returned, the tuples fetched and the blocks fetched of the tables and indexes,
# the transaction's own in a transaction and the flushed totals around a statement outside one). A lookup that scans
# the agent's timeline for a sub-agent's call shows up as reads that grow with the rows stored. The reads, the bytes the
# reader reads and the work in statements per round do not grow with what is already stored (the slope of a
# straight line through the rounds is flat), the memory of the writer and the projector is within its bounds after
# every round, the stored rows are the whitelisted ones only, and a restart reads back only the rows after the last
# quiet point. The same run with a call cache too small for the calls shows the calls found through the index.
# meiva:link agent/harness_timeline/README.md

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Self
from uuid import uuid4

from psycopg import AsyncCursor
from psycopg_pool import AsyncConnectionPool

from agent.harness_timeline.plan import ReplyPolicy
from agent.harness_timeline.writer import (
    CALLS_KEPT,
    CLOSED_KEPT,
    DELIVERED_KEPT,
    NON_HUMAN_LIMIT,
    TURN_USAGE_LIMIT,
    Committed,
    HarnessTimelineWriter,
)
from base.clock.tests.fakes import system_clock
from base.lm.harness.claude.transcript.reader import ClaudeRecordReader
from base.lm.harness.claude.transcript.tests.support import FIXTURES, now_ns, rows
from base.lm.harness.session.transcript.projection import (
    CALLS_LIMIT,
    SEEN_LIMIT,
    TURNS_LIMIT,
    USAGE_LIMIT,
)
from base.native_process.runtime_incarnation import RuntimeIncarnation

TURNS = 160
SUBAGENTS = 40
SUB_ROWS_PER_ROUND = 2
_RELATIONS = "FROM pg_class WHERE relnamespace = 'public'::regnamespace AND relkind IN ('r', 'i')"
_IN_TRANSACTION = (
    "SELECT COALESCE(sum(pg_stat_get_xact_tuples_returned(oid) + pg_stat_get_xact_tuples_fetched(oid) "
    f"+ pg_stat_get_xact_blocks_fetched(oid)), 0)::bigint {_RELATIONS}"
)
_FLUSHED = (
    "SELECT COALESCE(sum(pg_stat_get_tuples_returned(oid) + pg_stat_get_tuples_fetched(oid) "
    f"+ pg_stat_get_blocks_fetched(oid)), 0)::bigint {_RELATIONS}"
)


class StatementLog:
    def __init__(self) -> None:
        self.statements: list[tuple[str, int, int]] = []
        self.call_lookups = 0

    def drain(self) -> list[tuple[str, int, int]]:
        taken, self.statements = self.statements, []
        return taken


LOG = StatementLog()


# meiva:note
# What the database has read so far for the relations of the schema: this transaction's own counters inside a
# transaction, and the totals (flushed first: a backend reports its counters only at the end of a transaction) outside
# one, where a statement is a transaction of its own that has ended when the probe runs.
async def _reads(cursor: AsyncCursor[tuple[object, ...]]) -> int:
    in_transaction = cursor.connection.info.transaction_status.name == "INTRANS"
    async with AsyncCursor(cursor.connection) as probe:
        if not in_transaction:
            await probe.execute("SELECT pg_stat_force_next_flush()")
        await probe.execute(_IN_TRANSACTION if in_transaction else _FLUSHED)
        row = await probe.fetchone()
    assert row is not None
    return int(str(row[0]))


class CountingCursor(AsyncCursor[tuple[object, ...]]):
    async def execute(self, *args: Any, **kwargs: Any) -> Self:
        before = await _reads(self)
        result = await super().execute(*args, **kwargs)
        await self._record(args[0], before)
        return result

    async def executemany(self, *args: Any, **kwargs: Any) -> None:
        before = await _reads(self)
        await super().executemany(*args, **kwargs)
        await self._record(args[0], before)

    async def _record(self, query: object, before: int) -> None:
        text = query if isinstance(query, str) else str(query)
        after = await _reads(self)
        LOG.statements.append(
            (text.lstrip().split(None, 1)[0].upper(), max(self.rowcount, 0), after - before)
        )
        LOG.call_lookups += "call_id = ANY" in text


def _line(row: dict[str, object]) -> str:
    return json.dumps(row)


def _main_turn(lines: list[str], turn: int) -> list[str]:
    out: list[str] = []
    for line in lines:
        row = json.loads(line)
        if "uuid" in row:
            row["uuid"] = f"{row['uuid']}-{turn}"
        if isinstance(row.get("promptId"), str):
            row["promptId"] = f"{row['promptId']}-{turn}"
        message = row.get("message")
        if isinstance(message, dict) and "id" in message:
            message["id"] = f"{message['id']}-{turn}"
        out.append(_line(row))
        out.append(_line({"type": "api-request-blob", "uuid": f"b-{turn}", "x": "r" * 300}))
    return out


def _launch(turn: int, sub: int) -> list[str]:
    call = f"toolu-bg-{sub}"
    stamp = f"2026-11-12T09:{turn % 60:02d}:00.000Z"
    return [
        _line(
            {
                "type": "assistant",
                "uuid": f"launch-{sub}",
                "timestamp": stamp,
                "isSidechain": False,
                "message": {
                    "id": f"msg-launch-{sub}",
                    "model": "claude-haiku-5-5",
                    "role": "assistant",
                    "stop_reason": "tool_use",
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                    "content": [{"type": "tool_use", "id": call, "name": "Agent", "input": {}}],
                },
            }
        ),
        _line(
            {
                "type": "user",
                "uuid": f"launched-{sub}",
                "timestamp": stamp,
                "isSidechain": False,
                "toolUseResult": {"agentId": f"bg{sub}", "isAsync": True},
                "message": {
                    "role": "user",
                    "content": [{"type": "tool_result", "tool_use_id": call, "content": "started"}],
                },
            }
        ),
    ]


def _killed_call(turn: int) -> str:
    return _line(
        {
            "type": "assistant",
            "uuid": f"killed-{turn}",
            "timestamp": f"2026-11-12T10:{turn % 60:02d}:00.000Z",
            "isSidechain": False,
            "message": {
                "id": f"msg-killed-{turn}",
                "model": "claude-haiku-5-5",
                "role": "assistant",
                "stop_reason": "tool_use",
                "usage": {"input_tokens": 1, "output_tokens": 1},
                "content": [
                    {"type": "tool_use", "id": f"toolu-killed-{turn}", "name": "Bash", "input": {}}
                ],
            },
        }
    )


def _sub_rows(turn: int, sub: int) -> list[str]:
    return [
        _line(
            {
                "type": "assistant",
                "uuid": f"sub{sub}-{turn}-{n}",
                "timestamp": f"2026-11-12T11:{turn % 60:02d}:{n:02d}.000Z",
                "isSidechain": True,
                "agentId": f"bg{sub}",
                "message": {
                    "id": f"msg-sub{sub}-{turn}-{n}",
                    "model": "claude-haiku-5-5",
                    "role": "assistant",
                    "stop_reason": "end_turn",
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                    "content": [{"type": "text", "text": f"sub-agent {sub} progress {turn}.{n}"}],
                },
            }
        )
        for n in range(SUB_ROWS_PER_ROUND)
    ]


# meiva:note
# Checked after every round, not once at the end: what the writer and the projector hold is within its bounds at each
# point of the run. What is kept per record (a link, a position) is at most one per sub-agent that has started so far.
def _remembers_no_more_than_its_bounds(
    writer: HarnessTimelineWriter, turn: int, calls_kept: int
) -> None:
    projector, own = writer.projector_sizes(), writer.memory_sizes()
    started = min(turn + 1, SUBAGENTS)
    assert projector["seen"] <= SEEN_LIMIT and projector["calls"] <= CALLS_LIMIT, (turn, projector)
    assert projector["turns"] <= TURNS_LIMIT and projector["usage"] <= USAGE_LIMIT, (
        turn,
        projector,
    )
    assert projector["links"] <= started, (turn, "one link per sub-agent, however long they run")
    assert projector["orphans"] == 0 and projector["held"] == 0, (turn, "nothing is held for ever")
    assert own["calls"] <= calls_kept and own["positions"] <= started + 1, (turn, own)
    assert own["sent_items"] == own["sent_inbound"] == own["sent_wire"] == 0, own
    assert (
        own["non_human"] <= NON_HUMAN_LIMIT
        and own["closed"] <= CLOSED_KEPT
        and own["delivered"] <= DELIVERED_KEPT
    ), own
    assert own["turn_usage"] <= TURN_USAGE_LIMIT, own


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
    reader: ClaudeRecordReader,
    calls_kept: int = CALLS_KEPT,
) -> HarnessTimelineWriter:
    del home
    writer = HarnessTimelineWriter(
        calls_kept=calls_kept,
        pool=pool,
        agent_id=agent_id,
        harness="claude_code",
        session_id=sid,
        marker_id=lambda: "m",
        incarnation=incarnation,
        reader=reader,
        policy=ReplyPolicy(explicit_visibility=False, delivery_mode=None),
        render_tool=lambda name, tool_input: f"{name}{sorted(tool_input)}",
        after_commit=_noop,
        cli_version=lambda: None,
        clock=system_clock(),
    )
    writer.start_reading()
    return writer


def _volume(statements: list[tuple[str, int, int]]) -> tuple[int, int, int, int]:
    read = sum(rows_ for kind, rows_, _ in statements if kind == "SELECT")
    written = sum(rows_ for kind, rows_, _ in statements if kind in ("INSERT", "UPDATE"))
    return read, written, len(statements), sum(reads for *_, reads in statements)


# meiva:note
# The cost per round does not grow with the rounds: the slope of the least-squares line through the rounds (the rounds
# while the sub-agents are still being started are left out, and the last is a partial batch) carried over the span of
# the rounds is no more than a quarter of the mean cost, plus `slack`. A cost that grows with what is stored (a scan, a lookup of
# every call, a re-read) has a slope of the order of the mean itself; a constant has none.
def _does_not_grow(per_round: list[tuple[int, ...]], slack: int, label: str) -> None:
    full = per_round[SUBAGENTS + 10 : -1]
    n = len(full)
    for column in range(len(full[0])):
        values = [batch[column] for batch in full]
        mean = sum(values) / n
        centre = (n - 1) / 2
        slope = sum((i - centre) * (v - mean) for i, v in enumerate(values)) / sum(
            (i - centre) ** 2 for i in range(n)
        )
        assert slope * n <= 0.25 * mean + slack, (
            label,
            column,
            round(slope, 2),
            round(mean, 1),
            values,
        )


async def _agent(pool: AsyncConnectionPool[Any]) -> tuple[int, RuntimeIncarnation]:
    async with pool.connection() as conn:
        cursor = await conn.execute("INSERT INTO agents DEFAULT VALUES RETURNING id")
        agent_id = await _scalar(cursor)
        generation, owner = uuid4(), uuid4()
        await conn.execute(
            "INSERT INTO agents_meta (id, status, harness, runtime_generation, runtime_owner) "
            "VALUES (%s, 'idling', 'claude_code', %s, %s)",
            (agent_id, generation, owner),
        )
    return agent_id, RuntimeIncarnation(agent_id, generation, owner)


def _append_round(target: Path, folder: Path, basic: list[str], turn: int) -> None:
    main = _main_turn(basic, turn)
    main = [*main[:16], _killed_call(turn), *main[16:]]
    if turn < SUBAGENTS:
        main = [*main, *_launch(turn, turn)]
    with target.open("a") as handle:
        handle.write("\n".join(main) + "\n")
    for sub in range(min(turn + 1, SUBAGENTS)):
        with (folder / f"agent-bg{sub}.jsonl").open("a") as handle:
            handle.write("\n".join(_sub_rows(turn, sub)) + "\n")


async def _stored_facts(pool: AsyncConnectionPool[Any], agent_id: int) -> int:
    async with pool.connection() as conn:
        cursor = await conn.execute(
            "SELECT count(*) FROM agent_harness_transcript_rows WHERE agent_id = %s "
            "AND row->>'type' LIKE 'api-request%%'",
            (agent_id,),
        )
        assert await cursor.fetchone() == (0,)
        cursor = await conn.execute(
            "SELECT count(*) FROM agent_harness_messages WHERE agent_id = %s "
            "AND item->>'kind' = 'subagent_text' AND item->>'parent_item_id' IS NULL",
            (agent_id,),
        )
        assert await cursor.fetchone() == (0,), "every sub-agent item nests under its call"
        cursor = await conn.execute(
            "SELECT rebuild_ordinal FROM agent_harness_transcript_positions "
            "WHERE agent_id = %s AND record = 'main'",
            (agent_id,),
        )
        rebuild = await cursor.fetchone()
        assert rebuild is not None and int(str(rebuild[0])) > 0, "the quiet point moves on"
        cursor = await conn.execute(
            "SELECT count(*) FROM agent_harness_transcript_rows WHERE agent_id = %s", (agent_id,)
        )
        return await _scalar(cursor)


async def _long_session(db_url: str, tmp_path: Path, calls_kept: int) -> None:
    session = rows("basic")[0]["sessionId"]
    assert isinstance(session, str)
    LOG.call_lookups = 0
    target = tmp_path / "projects" / "p" / f"{session}.jsonl"
    folder = target.parent / session / "subagents"
    folder.mkdir(parents=True)
    target.write_text("")
    basic = (FIXTURES / "basic" / "main.jsonl").read_text().splitlines()
    pool = AsyncConnectionPool(
        db_url,
        min_size=1,
        max_size=2,
        kwargs={"autocommit": True, "prepare_threshold": None, "cursor_factory": CountingCursor},
        open=False,
    )
    await pool.open()
    try:
        agent_id, incarnation = await _agent(pool)
        reader = ClaudeRecordReader(now_ns=now_ns, claude_home=tmp_path, session_id=session)
        writer = _writer(pool, agent_id, incarnation, tmp_path, session, reader, calls_kept)
        per_round: list[tuple[int, ...]] = []
        read_bytes: list[tuple[int, ...]] = []
        for turn in range(TURNS):
            _append_round(target, folder, basic, turn)
            _ = LOG.drain()
            before = reader.bytes_read
            while await writer.read_now():
                pass
            per_round.append(_volume(LOG.drain()))
            read_bytes.append((reader.bytes_read - before,))
            _remembers_no_more_than_its_bounds(writer, turn, calls_kept)
        _does_not_grow(per_round, 40, "rows read, written, statements and what the database read")
        _does_not_grow(read_bytes, 100_000, "bytes read from the CLI's files")
        assert max(batch[3] for batch in per_round) < 8_000, ("database reads", per_round)
        stored = await _stored_facts(pool, agent_id)
        assert stored > 2_000
        _ = LOG.drain()
        restarted_reader = ClaudeRecordReader(
            now_ns=now_ns, claude_home=tmp_path, session_id=session
        )
        restarted = _writer(
            pool, agent_id, incarnation, tmp_path, session, restarted_reader, calls_kept
        )
        assert not await restarted.read_now()
        read, written, _, db_reads = _volume(LOG.drain())
        assert written <= 5 and read < stored // 10 and db_reads < stored, (
            read,
            written,
            stored,
            db_reads,
        )
    finally:
        await pool.close()


async def test_a_long_session_costs_constant_work_per_round_and_a_restart_reads_only_its_tail(
    db_url: str, tmp_path: Path
) -> None:
    await _long_session(db_url, tmp_path, CALLS_KEPT)
    assert LOG.call_lookups == 0, "every call a sub-agent nests under was in the writer's cache"


async def test_calls_that_fell_out_of_the_cache_are_found_through_the_index_at_constant_cost(
    db_url: str, tmp_path: Path
) -> None:
    await _long_session(db_url, tmp_path, 8)
    assert LOG.call_lookups > SUBAGENTS, (
        "the cache held 8 of the 40 calls, so the others were looked up"
    )

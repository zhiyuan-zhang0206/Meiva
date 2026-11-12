# meiva:note
# The writer's whole chain is linear in the length of the conversation, measured where a quadratic term would hide:
# in what the database scans, not in the rows it returns. A long session (a main record of hundreds of turns, forty
# background sub-agents that keep producing, calls that are never answered) is fed to the real reader and writer in
# rounds, and for every statement the writer sends inside a transaction the rows read by sequential scans
# (`seq_tup_read`) and fetched through indexes (`idx_tup_fetch`) are taken from `pg_stat_xact_user_tables` of that
# transaction. A lookup that scans the agent's timeline for a sub-agent's call (a LIMIT 1 does not stop a scan that
# has to filter) shows up as scans that grow with the rows stored. The scans, the bytes the reader reads, the work in
# statements and the memory of the writer and the projector per round do not grow with what is already stored, the
# stored rows are the whitelisted ones only, and a restart reads back only the rows after the last quiet point.
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
    CLOSED_USAGE_KEPT,
    NON_HUMAN_LIMIT,
    TURN_USAGE_LIMIT,
    Committed,
    HarnessTimelineWriter,
)
from base.clock.tests.fakes import system_clock
from base.lm.harness.claude.transcript.reader import ClaudeRecordReader
from base.lm.harness.claude.transcript.tests.support import FIXTURES, rows
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
_STATS = (
    "SELECT COALESCE(sum(seq_tup_read), 0)::bigint, COALESCE(sum(idx_tup_fetch), 0)::bigint "
    "FROM pg_stat_xact_user_tables"
)


class StatementLog:
    def __init__(self) -> None:
        self.statements: list[tuple[str, int, int, int]] = []

    def drain(self) -> list[tuple[str, int, int, int]]:
        taken, self.statements = self.statements, []
        return taken


LOG = StatementLog()


async def _scanned(cursor: AsyncCursor[tuple[object, ...]]) -> tuple[int, int] | None:
    if cursor.connection.info.transaction_status.name != "INTRANS":
        return None
    async with AsyncCursor(cursor.connection) as probe:
        await probe.execute(_STATS)
        row = await probe.fetchone()
    assert row is not None
    return int(str(row[0])), int(str(row[1]))


class CountingCursor(AsyncCursor[tuple[object, ...]]):
    async def execute(self, *args: Any, **kwargs: Any) -> Self:
        before = await _scanned(self)
        result = await super().execute(*args, **kwargs)
        await self._record(args[0], before)
        return result

    async def executemany(self, *args: Any, **kwargs: Any) -> None:
        before = await _scanned(self)
        await super().executemany(*args, **kwargs)
        await self._record(args[0], before)

    async def _record(self, query: object, before: tuple[int, int] | None) -> None:
        after = await _scanned(self)
        text = query if isinstance(query, str) else str(query)
        seq = idx = 0
        if before is not None and after is not None:
            seq, idx = after[0] - before[0], after[1] - before[1]
        LOG.statements.append(
            (text.lstrip().split(None, 1)[0].upper(), max(self.rowcount, 0), seq, idx)
        )


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


def _remembers_no_more_than_its_bounds(writer: HarnessTimelineWriter) -> None:
    projector, own = writer.projector_sizes(), writer.memory_sizes()
    assert projector["seen"] <= SEEN_LIMIT and projector["calls"] <= CALLS_LIMIT, projector
    assert projector["turns"] <= TURNS_LIMIT and projector["usage"] <= USAGE_LIMIT, projector
    assert projector["links"] == SUBAGENTS, "one link per sub-agent, however long they run"
    assert projector["orphans"] == 0 and projector["held"] == 0, "nothing is held for ever"
    assert own["calls"] <= CALLS_KEPT and own["positions"] == SUBAGENTS + 1, own
    assert own["sent_items"] == own["sent_inbound"] == own["sent_wire"] == 0, own
    assert own["non_human"] <= NON_HUMAN_LIMIT and own["closed_usages"] <= CLOSED_USAGE_KEPT, own
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
) -> HarnessTimelineWriter:
    del home
    writer = HarnessTimelineWriter(
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


def _volume(
    statements: list[tuple[str, int, int, int]],
) -> tuple[int, int, int, int, int]:
    read = sum(rows_ for kind, rows_, _, _ in statements if kind == "SELECT")
    written = sum(rows_ for kind, rows_, _, _ in statements if kind in ("INSERT", "UPDATE"))
    return (
        read,
        written,
        len(statements),
        sum(s for *_, s, _ in statements),
        sum(i for *_, i in statements),
    )


def _later_is_no_worse(per_round: list[tuple[int, ...]], slack: int, label: str) -> None:
    full = per_round[:-1]
    early, late = full[45:65], full[-20:]
    for column in range(len(early[0])):
        assert (
            max(batch[column] for batch in late)
            <= 1.5 * max(batch[column] for batch in early) + slack
        ), (label, column, early, late)


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


async def test_a_long_session_costs_constant_work_per_round_and_a_restart_reads_only_its_tail(
    db_url: str, tmp_path: Path
) -> None:
    session = rows("basic")[0]["sessionId"]
    assert isinstance(session, str)
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
        reader = ClaudeRecordReader(claude_home=tmp_path, session_id=session)
        writer = _writer(pool, agent_id, incarnation, tmp_path, session, reader)
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
        _later_is_no_worse(per_round, 40, "scanned rows and statements")
        _later_is_no_worse(read_bytes, 1_000_000, "bytes read from the CLI's files")
        _remembers_no_more_than_its_bounds(writer)
        assert max(batch[3] for batch in per_round) < 5_000, ("sequential scan reads", per_round)
        stored = await _stored_facts(pool, agent_id)
        assert stored > 2_000
        _ = LOG.drain()
        restarted_reader = ClaudeRecordReader(claude_home=tmp_path, session_id=session)
        restarted = _writer(pool, agent_id, incarnation, tmp_path, session, restarted_reader)
        assert not await restarted.read_now()
        read, written, _, _, _ = _volume(LOG.drain())
        assert written <= 5 and read < stored // 10, (read, written, stored)
    finally:
        await pool.close()

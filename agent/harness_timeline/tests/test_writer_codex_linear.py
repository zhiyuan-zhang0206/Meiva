from __future__ import annotations

import copy
from pathlib import Path
from typing import Any
from uuid import uuid4

from psycopg import sql
from psycopg_pool import AsyncConnectionPool

from agent.harness_timeline.plan import ReplyPolicy
from agent.harness_timeline.tests.support import send_input
from agent.harness_timeline.tests.test_writer_linear import LOG, CountingCursor
from agent.harness_timeline.writer import Committed, HarnessTimelineWriter
from base.clock.tests.fakes import system_clock
from base.lm.harness.codex.transcript.reader import CodexRecordReader
from base.lm.harness.codex.transcript.shapes import Obj
from base.lm.harness.codex.transcript.tests.rig import STAMP, THREAD, Rig, basic
from base.lm.harness.codex.transcript.tests.support import row_item
from base.lm.harness.session.transcript.entries import MAIN_RECORD
from base.native_process.runtime_incarnation import RuntimeIncarnation

# meiva:note
# The Codex path is linear in the length of the conversation, measured the way the Claude path is: in what the
# database reads and writes per round, in the requests the reader makes and in the bytes of the rollout it reads. A
# long thread (a hundred and twenty turns, each an input, a command and a reply, with ten child threads that each
# add a message every round) is fed to the real reader and the real writer round by round; the figures of a round
# must not grow with the rounds (the slope of a straight line through them, carried over the span, is no more than
# a quarter of the mean plus a slack), the reader's requests per round are bounded however many children
# have run, and a restarted writer reads back only the rows after its last quiet point. The calls that return a whole
# history are never made (the reader's own test checks its source; the fake serves only item pages).
# meiva:link agent/harness_timeline/README.md

ROUNDS = 120
CHILDREN = 10
WARMUP = CHILDREN + 12
MAIN_TEXT = "x" * 300


def _item(base: Obj, kind: str, ident: str, turn: str, **fields: object) -> Obj:
    row = copy.deepcopy(base)
    row["turnId"] = turn
    item = row_item(row)
    item.clear()
    item.update({"type": kind, "id": ident, "startedAtMs": 1, "completedAtMs": 2, **fields})
    return row


def _usage(response: str, turn: str, ordinal: int) -> Obj:
    return {
        "type": "token_usage_record",
        "timestamp": STAMP,
        "ordinal": ordinal,
        "payload": {
            "response_id": response,
            "turn_id": turn,
            "usage": {"input_tokens": 5, "output_tokens": 2},
        },
    }


def _done(turn: str, ordinal: int) -> Obj:
    return {
        "type": "event_msg",
        "timestamp": STAMP,
        "ordinal": ordinal,
        "payload": {"type": "task_complete", "turn_id": turn},
    }


def _volume(statements: list[tuple[str, int, int]]) -> tuple[int, ...]:
    read = sum(rows for kind, rows, _ in statements if kind == "SELECT")
    written = sum(rows for kind, rows, _ in statements if kind in ("INSERT", "UPDATE"))
    return read, written, len(statements), sum(reads for *_, reads in statements)


def _does_not_grow(per_round: list[tuple[int, ...]], slack: int, label: str) -> None:
    full = per_round[WARMUP:-1]
    n = len(full)
    for column in range(len(full[0])):
        values = [batch[column] for batch in full]
        mean = sum(values) / n
        centre = (n - 1) / 2
        slope = sum((i - centre) * (v - mean) for i, v in enumerate(values)) / sum(
            (i - centre) ** 2 for i in range(n)
        )
        assert slope * n <= 0.25 * mean + slack, (label, column, round(slope, 3), round(mean, 1))


async def _agent(pool: AsyncConnectionPool[Any]) -> tuple[int, RuntimeIncarnation]:
    async with pool.connection() as conn:
        cursor = await conn.execute("INSERT INTO agents DEFAULT VALUES RETURNING id")
        row = await cursor.fetchone()
        assert row is not None
        agent_id = int(str(row[0]))
        generation, owner = uuid4(), uuid4()
        await conn.execute(
            "INSERT INTO agents_meta (id, status, harness, runtime_generation, runtime_owner) "
            "VALUES (%s, 'idling', 'codex', %s, %s)",
            (agent_id, generation, owner),
        )
    return agent_id, RuntimeIncarnation(agent_id, generation, owner)


async def _noop(commit: Committed) -> None:
    del commit


def _writer(
    pool: AsyncConnectionPool[Any],
    agent_id: int,
    incarnation: RuntimeIncarnation,
    reader: CodexRecordReader,
) -> HarnessTimelineWriter:
    writer = HarnessTimelineWriter(
        pool=pool,
        agent_id=agent_id,
        harness="codex",
        session_id=THREAD,
        wire_id=reader.wire_id,
        marker_id=lambda: "m",
        incarnation=incarnation,
        reader=reader,
        policy=ReplyPolicy(explicit_visibility=False, delivery_mode=None),
        render_tool=lambda name, tool_input: f"{name}{sorted(tool_input)}",
        after_commit=_noop,
        cli_version=lambda: "0.166.0",
        clock=system_clock(),
    )
    writer.start_reading()
    return writer


def _main_items(base: Obj, turn: int, children: list[str]) -> list[Obj]:
    name = f"t-{turn}"
    spawned = [
        _item(
            base,
            "collabAgentToolCall",
            f"spawn-{i}",
            name,
            tool="spawnAgent",
            status="completed",
            receiverThreadIds=[child],
            agentsStates={},
        )
        for i, child in enumerate(children)
        if turn == 0
    ]
    command = _item(
        base,
        "commandExecution",
        f"exec-{turn}",
        name,
        command="ls",
        cwd="/w",
        status="completed",
        aggregatedOutput=MAIN_TEXT,
        exitCode=0,
    )
    return [
        *spawned,
        _item(base, "userMessage", f"u-{turn}", name, clientId=f"c-{turn}", content=[]),
        command,
        _item(base, "agentMessage", f"m-{turn}", name, text=MAIN_TEXT),
    ]


async def _analyze(pool: AsyncConnectionPool[Any]) -> None:
    async with pool.connection() as conn:
        await conn.execute("ANALYZE inbound_messages")


async def _inbound(pool: AsyncConnectionPool[Any], agent_id: int) -> int:
    async with pool.connection() as conn:
        cursor = await conn.execute(
            "INSERT INTO inbound_messages (agent_id, content, kind, source, status) "
            "VALUES (%s, 'hi', 'chat', 'web', 'claimed') RETURNING id",
            (agent_id,),
        )
        row = await cursor.fetchone()
    assert row is not None
    return int(str(row[0]))


async def test_each_round_costs_the_same_however_long_the_thread_is_and_a_restart_reads_its_tail(
    db_url: str, tmp_path: Path
) -> None:
    rows, _ = basic()
    base = rows[2]
    rig = Rig(tmp_path)
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
        reader = rig.restarted_reader()
        writer = _writer(pool, agent_id, incarnation, reader)
        children = [f"child-{i}" for i in range(CHILDREN)]
        for child in children:
            rig.add_child(child)
        per_round: list[tuple[int, ...]] = []
        requests: list[tuple[int, ...]] = []
        rollout_bytes: list[tuple[int, ...]] = []
        for turn in range(ROUNDS):
            name = f"t-{turn}"
            main = _main_items(base, turn, children)
            await _analyze(pool)
            _ = LOG.drain()
            before = (reader.requests, reader.rollout_bytes_read)
            inbound = await _inbound(pool, agent_id)
            send_input(writer, f"c-{turn}", f"c-{turn}", inbound)
            rig.feed(main, [_usage(f"r-{turn}", name, 10_000 + turn), _done(name, 20_000 + turn)])
            for index, child in enumerate(children):
                rig.feed_child(
                    child,
                    [_item(base, "agentMessage", f"cm-{index}-{turn}", f"ct-{index}", text="c")],
                    [_usage(f"cr-{index}-{turn}", f"ct-{index}", 30_000 + turn)],
                )
            while await writer.read_now():
                pass
            per_round.append(_volume(LOG.drain()))
            requests.append((reader.requests - before[0],))
            rollout_bytes.append((reader.rollout_bytes_read - before[1],))
        _figures_are_flat(per_round, requests, rollout_bytes)
        assert writer.closed_turns == ROUNDS
        assert await _statuses(pool, agent_id) == ["done"] * ROUNDS
        _ = await writer.read_now()
        assert await reader.records() == (MAIN_RECORD,), "finished children are not asked about"
        await _restart_reads_its_tail(pool, agent_id, incarnation, rig)
    finally:
        await pool.close()


def _figures_are_flat(
    per_round: list[tuple[int, ...]],
    requests: list[tuple[int, ...]],
    rollout_bytes: list[tuple[int, ...]],
) -> None:
    _does_not_grow(per_round, 40, "rows read, written, statements and database reads")
    _does_not_grow(requests, 4, "requests of the reader")
    _does_not_grow(rollout_bytes, 4_000, "bytes of the rollouts")
    assert max(batch[0] for batch in requests) <= 3 * (CHILDREN + 1) + 2, requests
    assert max(batch[3] for batch in per_round) < 6_000, per_round


async def _restart_reads_its_tail(
    pool: AsyncConnectionPool[Any], agent_id: int, incarnation: RuntimeIncarnation, rig: Rig
) -> None:
    stored = await _count(pool, "agent_harness_transcript_rows", agent_id)
    assert stored > ROUNDS * 4
    assert await _count(pool, "agent_harness_transcript_positions", agent_id) == CHILDREN + 1
    _ = LOG.drain()
    restarted = _writer(pool, agent_id, incarnation, rig.restarted_reader())
    assert not await restarted.read_now()
    read, written, _, db_reads = _volume(LOG.drain())
    assert written <= 5 and read < stored // 10 and db_reads < stored, (
        read,
        written,
        stored,
        db_reads,
    )


async def _statuses(pool: AsyncConnectionPool[Any], agent_id: int) -> list[str]:
    async with pool.connection() as conn:
        cursor = await conn.execute(
            "SELECT status FROM inbound_messages WHERE agent_id = %s ORDER BY id", (agent_id,)
        )
        return [str(row[0]) for row in await cursor.fetchall()]


async def _count(pool: AsyncConnectionPool[Any], table: str, agent_id: int) -> int:
    query = sql.SQL("SELECT count(*) FROM {} WHERE agent_id = %s").format(sql.Identifier(table))
    async with pool.connection() as conn:
        cursor = await conn.execute(query, (agent_id,))
        row = await cursor.fetchone()
        assert row is not None
        return int(str(row[0]))

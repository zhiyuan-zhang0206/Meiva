from __future__ import annotations

import shutil
from pathlib import Path
from uuid import uuid4

import pytest
from psycopg_pool import AsyncConnectionPool

from agent.harness_timeline.tests.support import (
    BASIC,
    SUBS,
    ScriptedReader,
    make_agent,
    make_inbound_row,
    make_writer,
    send_input,
    status_of,
    timeline_of,
)
from agent.harness_timeline.writer import Committed, TurnEndWait
from base.lm.harness.claude.transcript.reader import ClaudeRecordReader
from base.lm.harness.claude.transcript.tests.support import FIXTURES, entries_of, rows
from base.native_process.runtime_incarnation import RuntimeIncarnation


async def test_a_turn_is_written_once_in_read_order_and_settles_its_input(
    aops_pool: AsyncConnectionPool,
) -> None:
    agent_id, incarnation = await make_agent(aops_pool)
    row = await make_inbound_row(aops_pool, agent_id)
    reader = ScriptedReader({"main": entries_of("basic")})
    committed: list[Committed] = []
    writer = make_writer(aops_pool, agent_id, incarnation, reader, committed)
    send_input(writer, BASIC, "k1", row)
    reader.visible["main"] = 5
    assert await writer.read_now()
    reader.visible["main"] = len(reader.records_data["main"])
    assert await writer.read_now()
    assert not await writer.read_now()
    kinds = [kind for kind, _, _ in await timeline_of(aops_pool, agent_id)]
    assert kinds == [
        "inbound_chat",
        "agent_reasoning",
        "agent_code",
        "code_output",
        "agent_reasoning",
        "agent_chat",
    ]
    assert await status_of(aops_pool, row) == "done"
    assert sum(len(c.written) for c in committed) == 6
    assert writer.closed_turns == 1
    assert [(s.input_key) for c in committed for s in c.plan.settlements] == ["k1"]


async def test_a_requeued_row_proven_by_the_record_is_settled_not_delivered_again(
    aops_pool: AsyncConnectionPool,
) -> None:
    agent_id, incarnation = await make_agent(aops_pool)
    row = await make_inbound_row(aops_pool, agent_id, status="pending")
    reader = ScriptedReader({"main": entries_of("basic")})
    reader.visible["main"] = 6
    writer = make_writer(aops_pool, agent_id, incarnation, reader, [])
    send_input(writer, BASIC, "k1", row)
    assert await writer.read_now()
    assert await status_of(aops_pool, row) == "done"


async def test_a_restart_replays_silently_then_continues_after_the_stored_position(
    aops_pool: AsyncConnectionPool,
) -> None:
    agent_id, incarnation = await make_agent(aops_pool)
    row = await make_inbound_row(aops_pool, agent_id)
    reader = ScriptedReader({"main": entries_of("basic")})
    reader.visible["main"] = 6
    first = make_writer(aops_pool, agent_id, incarnation, reader, [])
    send_input(first, BASIC, "k1", row)
    assert await first.read_now()
    before = await timeline_of(aops_pool, agent_id)
    committed: list[Committed] = []
    second = make_writer(aops_pool, agent_id, incarnation, reader, committed)
    send_input(second, BASIC, "k1", row)
    assert not await second.read_now()
    assert await timeline_of(aops_pool, agent_id) == before
    reader.visible["main"] = len(reader.records_data["main"])
    assert await second.read_now()
    after = await timeline_of(aops_pool, agent_id)
    assert after[: len(before)] == before and len(after) == 6
    assert len({item_id for _, item_id, _ in after}) == len(after)


async def test_a_failed_read_changes_nothing_and_the_next_read_proceeds(
    aops_pool: AsyncConnectionPool,
) -> None:
    agent_id, incarnation = await make_agent(aops_pool)
    row = await make_inbound_row(aops_pool, agent_id)
    reader = ScriptedReader({"main": entries_of("basic")})
    reader.visible["main"] = 6
    writer = make_writer(aops_pool, agent_id, incarnation, reader, [])
    send_input(writer, BASIC, "k1", row)
    assert await writer.read_now() is True
    reader.fail_next = True
    with pytest.raises(OSError, match="could not be read"):
        _ = await writer.read_now()
    assert len(await timeline_of(aops_pool, agent_id)) == 4
    assert not await writer.read_now()


async def test_a_stale_incarnation_writes_nothing(aops_pool: AsyncConnectionPool) -> None:
    agent_id, _ = await make_agent(aops_pool)
    row = await make_inbound_row(aops_pool, agent_id)
    stale = RuntimeIncarnation(agent_id, uuid4(), uuid4())
    reader = ScriptedReader({"main": entries_of("basic")})
    reader.visible["main"] = 6
    writer = make_writer(aops_pool, agent_id, stale, reader, [])
    send_input(writer, BASIC, "k1", row)
    assert not await writer.read_now()
    assert writer.fenced
    assert await timeline_of(aops_pool, agent_id) == []
    assert await status_of(aops_pool, row) == "claimed"


async def test_sub_agent_rows_nest_under_the_call_item_even_when_read_in_a_later_pass(
    aops_pool: AsyncConnectionPool,
) -> None:
    agent_id, incarnation = await make_agent(aops_pool)
    row = await make_inbound_row(aops_pool, agent_id)
    main = entries_of("subagents")
    sync_sub = entries_of("subagents", "af6798fb938fb227f")
    async_sub = entries_of("subagents", "a0044dfe6d0ac4533")
    reader = ScriptedReader(
        {"main": main, "af6798fb938fb227f": sync_sub, "a0044dfe6d0ac4533": async_sub}
    )
    reader.visible.update(
        {"main": len(main), "af6798fb938fb227f": len(sync_sub), "a0044dfe6d0ac4533": 0}
    )
    writer = make_writer(aops_pool, agent_id, incarnation, reader, [])
    send_input(writer, SUBS, "k", row)
    assert await writer.read_now()
    reader.visible["a0044dfe6d0ac4533"] = len(async_sub)
    assert await writer.read_now()
    timeline = await timeline_of(aops_pool, agent_id)
    calls = {item_id for kind, item_id, _ in timeline if kind == "agent_code"}
    nested = [(kind, parent) for kind, _, parent in timeline if parent is not None]
    assert len(nested) == 6 and all(parent in calls for _, parent in nested)
    assert len({parent for _, parent in nested}) == 2


async def test_the_turn_end_wait_sees_a_closing_entry_or_gives_up_quietly(
    aops_pool: AsyncConnectionPool,
) -> None:
    agent_id, incarnation = await make_agent(aops_pool)
    reader = ScriptedReader({"main": entries_of("basic")})
    reader.visible["main"] = len(reader.records_data["main"])
    writer = make_writer(aops_pool, agent_id, incarnation, reader, [])
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
    agent_id, incarnation = await make_agent(aops_pool)
    row = await make_inbound_row(aops_pool, agent_id)
    reader = ClaudeRecordReader(claude_home=tmp_path, session_id=session)
    writer = make_writer(aops_pool, agent_id, incarnation, reader, [])
    send_input(writer, BASIC, "k1", row)
    _ = await writer.read_now()
    _ = shutil.copyfile(FIXTURES / "basic" / "main.jsonl", target)
    _ = await writer.read_now()
    restarted = make_writer(
        aops_pool,
        agent_id,
        incarnation,
        ClaudeRecordReader(claude_home=tmp_path, session_id=session),
        [],
    )
    send_input(restarted, BASIC, "k1", row)
    assert not await restarted.read_now()
    kinds = [kind for kind, _, _ in await timeline_of(aops_pool, agent_id)]
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
    agent_id, incarnation = await make_agent(aops_pool)
    row = await make_inbound_row(aops_pool, agent_id)
    entries = entries_of("basic")
    result_at = next(i for i, entry in enumerate(entries) if type(entry).__name__ == "ToolResult")
    reader = ScriptedReader({"main": entries})
    reader.visible["main"] = result_at
    first = make_writer(aops_pool, agent_id, incarnation, reader, [])
    send_input(first, BASIC, "k1", row)
    assert await first.read_now()
    assert await _rebuild_ordinal(aops_pool, agent_id) == -1
    kinds_before = [kind for kind, _, _ in await timeline_of(aops_pool, agent_id)]
    assert kinds_before[-1] == "agent_code"
    reader.visible["main"] = len(entries)
    second = make_writer(aops_pool, agent_id, incarnation, reader, [])
    send_input(second, BASIC, "k1", row)
    assert await second.read_now()
    kinds = [kind for kind, _, _ in await timeline_of(aops_pool, agent_id)]
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
    agent_id, incarnation = await make_agent(aops_pool)
    row = await make_inbound_row(aops_pool, agent_id)
    entries = entries_of("basic")
    reader = ScriptedReader({"main": entries})
    writer = make_writer(aops_pool, agent_id, incarnation, reader, [])
    send_input(writer, BASIC, "k1", row)
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

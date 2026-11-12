# meiva:note
# What keeps the writer's resources bounded and its end orderly: a transaction holds a bounded number of entries and
# the records not yet read wait for the next one, a position the file no longer holds is put at the record's end and
# reported (never retried for ever, never a fault), a stop asked for before the loop ran is not forgotten, and a
# writer that is retired reads and writes nothing more.
# meiva:link agent/harness_timeline/README.md

from __future__ import annotations

import asyncio

from psycopg_pool import AsyncConnectionPool

from agent.harness_timeline.tests.support import (
    ScriptedReader,
    make_agent,
    make_writer,
    timeline_of,
)
from base.lm.harness.claude.transcript.tests.support import entries_of
from base.lm.harness.session.transcript.entries import (
    AgentText,
    AnyEntry,
    Order,
    SubagentLink,
    ToolCall,
)
from base.log.tests.log_records import LogRecords
from base.log.tests.log_records import loguru_records as loguru_records


async def _positions(pool: AsyncConnectionPool) -> dict[str, str]:
    async with pool.connection() as conn:
        cursor = await conn.execute(
            "SELECT record, position FROM agent_harness_transcript_positions"
        )
        return {str(record): str(position) for record, position in await cursor.fetchall()}


def _linked_subagents(count: int) -> tuple[ScriptedReader, int]:
    main: list[AnyEntry] = []
    records: dict[str, list[AnyEntry]] = {"main": main}
    for index in range(count):
        main.append(
            ToolCall(
                key=f"c{index}",
                order=Order(2 * index),
                call_id=f"call-{index}",
                name="Task",
                tool_input={},
            )
        )
        main.append(
            SubagentLink(
                key=f"l{index}",
                order=Order(2 * index + 1),
                call_id=f"call-{index}",
                child_record=f"sub{index}",
            )
        )
        records[f"sub{index}"] = [
            AgentText(
                key=f"s{index}-{n}",
                order=Order(n),
                record=f"sub{index}",
                text="x",
                message_ref=f"m{index}-{n}",
            )
            for n in range(5)
        ]
    reader = ScriptedReader(records)
    for record, record_entries in records.items():
        reader.visible[record] = len(record_entries)
    return reader, len(main)


async def test_a_transaction_holds_a_bounded_number_of_entries_and_the_other_records_wait_their_turn(
    aops_pool: AsyncConnectionPool,
) -> None:
    agent_id, incarnation = await make_agent(aops_pool)
    reader, _ = _linked_subagents(10)
    writer = make_writer(aops_pool, agent_id, incarnation, reader, [], max_transaction_entries=30)

    assert await writer.read_now()
    first = await _positions(aops_pool)
    assert 1 < len(first) < 11, f"one transaction read only some of the 11 records: {first}"

    while await writer.read_now():
        pass
    assert len(await _positions(aops_pool)) == 11, "every record was read in the end"
    kinds = [kind for kind, _, _ in await timeline_of(aops_pool, agent_id)]
    assert kinds.count("subagent_text") == 50 and kinds.count("agent_code") == 10


async def test_a_position_the_file_no_longer_holds_is_put_at_the_end_and_reported_not_retried(
    aops_pool: AsyncConnectionPool, loguru_records: LogRecords
) -> None:
    agent_id, incarnation = await make_agent(aops_pool)
    entries = entries_of("basic")
    reader = ScriptedReader({"main": entries})
    writer = make_writer(aops_pool, agent_id, incarnation, reader, [])
    reader.visible["main"] = 3
    assert await writer.read_now()
    before = [kind for kind, _, _ in await timeline_of(aops_pool, agent_id)]

    reader.lose_position = True
    reader.visible["main"] = 5
    assert not await writer.read_now(), (
        "no fault, and nothing is read from a file that is not the one"
    )
    (event,) = loguru_records.events("harness_timeline_position_lost")
    assert event["extra"]["record"] == "main" and event["extra"]["reseated_at"] == "5"
    assert await _positions(aops_pool) == {"main": "5"}
    assert [k for k, _, _ in await timeline_of(aops_pool, agent_id)] == before

    reader.visible["main"] = len(entries)
    assert await writer.read_now(), "reading goes on from the new end"
    assert len(await timeline_of(aops_pool, agent_id)) > len(before)


async def test_a_stop_asked_for_before_the_loop_ran_is_not_forgotten(
    aops_pool: AsyncConnectionPool,
) -> None:
    agent_id, incarnation = await make_agent(aops_pool)
    writer = make_writer(aops_pool, agent_id, incarnation, ScriptedReader({"main": []}), [])
    writer.stop()
    writer.clear_fault()
    await asyncio.wait_for(writer.run(), timeout=5)


async def test_a_retired_writer_reads_and_writes_nothing_more(
    aops_pool: AsyncConnectionPool,
) -> None:
    agent_id, incarnation = await make_agent(aops_pool)
    reader = ScriptedReader({"main": entries_of("basic")})
    reader.visible["main"] = 3
    writer = make_writer(aops_pool, agent_id, incarnation, reader, [])
    await writer.retire()
    assert not await writer.read_now()
    await writer.write_marker("late", error=False)
    assert await timeline_of(aops_pool, agent_id) == []
    writer.bind_incarnation(None)
    assert not await writer.read_now(), "binding a new incarnation does not revive it"

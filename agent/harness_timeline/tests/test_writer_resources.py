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


async def test_the_main_record_is_always_read_first_and_only_the_sub_agents_rotate(
    aops_pool: AsyncConnectionPool,
) -> None:
    agent_id, incarnation = await make_agent(aops_pool)
    reader, _ = _linked_subagents(10)
    writer = make_writer(aops_pool, agent_id, incarnation, reader, [], max_transaction_entries=30)

    firsts: list[str] = []
    others: list[list[str]] = []
    for _ in range(6):
        reader.reads.clear()
        if not await writer.read_now():
            break
        firsts.append(reader.reads[0])
        others.append([record for record in reader.reads if record != "main"])
    assert firsts and set(firsts) == {"main"}, firsts
    assert all(others), (
        "every transaction reads a sub-agent record: the main one cannot starve them"
    )
    assert len({record for read in others for record in read}) > len(others[0]), "and they rotate"


async def test_a_retry_backoff_is_ended_by_a_stop_and_does_not_wait_it_out(
    aops_pool: AsyncConnectionPool,
) -> None:
    agent_id, incarnation = await make_agent(aops_pool)
    reader = ScriptedReader({"main": entries_of("basic")})
    reader.visible["main"] = 3
    reader.fail_next = True
    writer = make_writer(aops_pool, agent_id, incarnation, reader, [])
    run = asyncio.create_task(writer.run())
    while not reader.reads:
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.05)
    writer.stop()
    await asyncio.wait_for(run, timeout=1.0)
    assert reader.exhaustive_listings >= 1, (
        "the last read of a stopped writer is the exhaustive one"
    )


async def test_a_retire_does_not_wait_for_ever_for_a_read_that_does_not_come_back(
    aops_pool: AsyncConnectionPool, loguru_records: LogRecords
) -> None:
    agent_id, incarnation = await make_agent(aops_pool)
    reader = ScriptedReader({"main": entries_of("basic")})
    reader.visible["main"] = 3
    reader.block_listing = asyncio.Event()
    writer = make_writer(aops_pool, agent_id, incarnation, reader, [], retire_wait_seconds=0.2)
    stuck = asyncio.create_task(writer.read_now())
    await asyncio.sleep(0.05)
    await asyncio.wait_for(writer.retire(), timeout=2.0)
    assert loguru_records.events("harness_timeline_writer_retired")

    reader.block_listing.set()
    assert not await stuck, (
        "the read that was in flight finds the writer retired and commits nothing"
    )
    assert await timeline_of(aops_pool, agent_id) == []


async def test_a_record_started_again_after_a_lost_position_counts_above_the_stored_ordinals(
    aops_pool: AsyncConnectionPool,
) -> None:
    agent_id, incarnation = await make_agent(aops_pool)
    reader = ScriptedReader({"main": entries_of("basic")})
    writer = make_writer(aops_pool, agent_id, incarnation, reader, [])
    reader.visible["main"] = 4
    assert await writer.read_now()
    reader.lose_position = True
    reader.visible["main"] = 6
    assert not await writer.read_now()
    assert reader.floors[-1] == 3, "the highest ordinal stored for the record is the floor"

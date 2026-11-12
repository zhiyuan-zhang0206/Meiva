from __future__ import annotations

import asyncio
import json
import shutil
from collections.abc import Mapping, Sequence
from pathlib import Path
from uuid import uuid4

import psycopg
from psycopg_pool import AsyncConnectionPool

from agent.harness_timeline.store import (
    StoredPosition,
    aappend_transcript_rows,
    aset_transcript_position,
)
from agent.harness_timeline.tests.support import (
    BASIC,
    POLICY,
    ScriptedReader,
    make_agent,
    make_inbound_row,
    make_writer,
    send_input,
    status_of,
    timeline_of,
)
from agent.harness_timeline.writer import Committed, HarnessTimelineWriter
from agent.ownership.hosted import admit_hosted_runtime
from agent.ownership.tests.ownership_support import agent_row
from base.agents.history.transcript_timeline.items import ReplyPolicy
from base.clock.tests.fakes import system_clock
from base.db import Database
from base.lm.harness.claude.transcript.reader import ClaudeRecordReader
from base.lm.harness.claude.transcript.tests.support import FIXTURES, entries_of, now_ns, rows
from base.lm.harness.session.transcript.entries import AnyEntry
from base.lm.harness.session.transcript.ports import StoredRow
from base.native_process.runtime_incarnation import RuntimeIncarnation

MACHINE = "claim-test"

# meiva:note
# The question the plan left open (Q8): how does a writer with no graph invocation present get the runtime
# incarnation its commits are fenced by? It is the one the host admitted for the latest hosted turn, bound to the
# writer (`bind_incarnation`) at every turn. Evidence: `admit_hosted_runtime` keeps the row's generation while the
# same host owns it (a new admission by the same owner returns the same incarnation) and the settled row stays
# `idling` with that generation, so the writer keeps committing between turns with the incarnation it holds; a
# different owner's admission mints a new generation, which fences the old incarnation: it writes nothing until the
# new one is bound.


async def test_the_incarnation_of_the_latest_admission_stays_valid_between_turns_and_a_new_owner_fences_it(
    aops_pool: AsyncConnectionPool, db_conn: psycopg.Connection, database: Database
) -> None:
    agent_id = agent_row(db_conn)
    owner = uuid4()
    first = await admit_hosted_runtime(aops_pool, agent_id, MACHINE, owner, db=database)
    assert first is not None
    entries = entries_of("basic")
    reader = ScriptedReader({"main": entries})
    reader.visible["main"] = 3
    committed: list[Committed] = []
    writer = make_writer(aops_pool, agent_id, first, reader, committed)
    assert await writer.read_now()

    db_conn.execute("UPDATE agents_meta SET status='idling' WHERE id = %s", (agent_id,))
    db_conn.commit()
    reader.visible["main"] = 5
    assert await writer.read_now(), (
        "the idle row still accepts the incarnation it was admitted with"
    )

    again = await admit_hosted_runtime(aops_pool, agent_id, MACHINE, owner, db=database)
    assert again == first
    reader.visible["main"] = len(entries) - 1
    assert await writer.read_now()

    takeover = await admit_hosted_runtime(aops_pool, agent_id, MACHINE, uuid4(), db=database)
    assert takeover is not None and takeover.generation != first.generation
    before = len(await timeline_of(aops_pool, agent_id))
    reader.visible["main"] = len(entries)
    assert not await writer.read_now() and writer.fenced
    assert len(await timeline_of(aops_pool, agent_id)) == before
    writer.bind_incarnation(takeover)
    assert await writer.read_now()
    assert writer.closed_turns == 1


async def test_a_new_writer_reads_nothing_until_the_invocation_starts_it(
    aops_pool: AsyncConnectionPool,
) -> None:
    agent_id, incarnation = await make_agent(aops_pool)
    row = await make_inbound_row(aops_pool, agent_id)
    reader = ScriptedReader({"main": entries_of("basic")})
    reader.visible["main"] = 6
    writer = make_writer(aops_pool, agent_id, incarnation, reader, [], reading=False)
    assert not await writer.read_now()
    assert await timeline_of(aops_pool, agent_id) == []
    send_input(writer, BASIC, "k1", row)
    writer.start_reading()
    assert await writer.read_now()
    assert await status_of(aops_pool, row) == "done"


async def test_a_fenced_writer_keeps_its_plan_and_commits_it_once_a_valid_incarnation_is_bound(
    aops_pool: AsyncConnectionPool,
) -> None:
    agent_id, good = await make_agent(aops_pool)
    row = await make_inbound_row(aops_pool, agent_id)
    reader = ScriptedReader({"main": entries_of("basic")})
    reader.visible["main"] = 6
    writer = make_writer(
        aops_pool, agent_id, RuntimeIncarnation(agent_id, uuid4(), uuid4()), reader, []
    )
    send_input(writer, BASIC, "k1", row)
    assert not await writer.read_now() and writer.fenced
    assert await status_of(aops_pool, row) == "claimed"
    writer.bind_incarnation(good)
    assert await writer.read_now()
    assert await status_of(aops_pool, row) == "done"
    assert len(await timeline_of(aops_pool, agent_id)) == 4


async def test_catching_up_repeats_reads_without_waiting_for_a_wake_up(
    aops_pool: AsyncConnectionPool,
) -> None:
    agent_id, incarnation = await make_agent(aops_pool)
    row = await make_inbound_row(aops_pool, agent_id)
    reader = ScriptedReader({"main": entries_of("basic")})
    writer = make_writer(aops_pool, agent_id, incarnation, reader, [])
    send_input(writer, BASIC, "k1", row)
    task = asyncio.create_task(writer.run())
    try:
        for visible in (2, 4, len(reader.records_data["main"])):
            reader.visible["main"] = visible
            writer.wake()
            await asyncio.sleep(0.2)
        assert len(await timeline_of(aops_pool, agent_id)) == 6
    finally:
        writer.stop()
        await asyncio.wait_for(task, 5)


async def test_a_marker_is_appended_under_the_fence_and_published(
    aops_pool: AsyncConnectionPool,
) -> None:
    agent_id, incarnation = await make_agent(aops_pool)
    committed: list[Committed] = []
    writer = make_writer(aops_pool, agent_id, incarnation, ScriptedReader({"main": []}), committed)
    await writer.write_marker("Harness execution stopped: boom", error=True)
    await writer.write_marker("Cancellation unconfirmed", error=False)
    kinds = await timeline_of(aops_pool, agent_id)
    assert [kind for kind, _, _ in kinds] == ["system_marker", "system_marker"]
    assert len(committed) == 2 and writer.items_written == 2
    async with aops_pool.connection() as conn:
        cursor = await conn.execute(
            "SELECT item->>'payload' FROM agent_harness_messages WHERE agent_id=%s ORDER BY seq",
            (agent_id,),
        )
        assert [row[0] for row in await cursor.fetchall()] == [
            "error:Harness execution stopped: boom",
            "status:Cancellation unconfirmed",
        ]


async def test_the_input_is_registered_under_the_wire_id_the_adapter_names(
    aops_pool: AsyncConnectionPool,
) -> None:
    agent_id, incarnation = await make_agent(aops_pool)
    reader = ScriptedReader({"main": []})
    writer = HarnessTimelineWriter(
        pool=aops_pool,
        agent_id=agent_id,
        harness="claude_code",
        session_id="s",
        wire_id=lambda key: f"wire-of-{key}",
        marker_id=lambda: "m",
        incarnation=incarnation,
        reader=reader,
        policy=POLICY,
        render_tool=_render_name,
        after_commit=_ignore,
        cli_version=lambda: None,
        clock=system_clock(),
    )
    writer.register_input("k1", "text", [], (1,))
    assert [(sent.key, sent.wire_id) for sent in writer.undelivered()] == [("k1", "wire-of-k1")]
    writer.forget_sent("k1")
    assert writer.undelivered() == ()


class _SpyReader(ScriptedReader):
    def __init__(self, records: dict[str, list[AnyEntry]]) -> None:
        super().__init__(records)
        self.seen: list[int] = []

    def parse_stored(self, rows: Sequence[StoredRow]) -> tuple[AnyEntry, ...]:
        self.seen.extend(row.ordinal for row in rows)
        return super().parse_stored(rows)


async def _ignore(commit: Committed) -> None:
    del commit


def _render_name(name: str, tool_input: Mapping[str, object]) -> str:
    del tool_input
    return name


# meiva:note
# A fork's session file starts with the parent's history, and so does the file of an agent that was on the event
# path when the setting was turned on: the history is already on the agent's timeline, so it must not be projected
# again. With a stored position nothing changes; with none, an agent that has timeline items starts at the row of the
# first input this process sent (history before it is skipped, the row itself and everything after it is read).
async def test_a_session_that_starts_with_history_projects_only_from_the_first_input_it_sent(
    aops_pool: AsyncConnectionPool, tmp_path: Path
) -> None:
    session = rows("basic")[0]["sessionId"]
    assert isinstance(session, str)
    folder = tmp_path / "projects" / "p"
    folder.mkdir(parents=True)
    target = folder / f"{session}.jsonl"
    source = (FIXTURES / "basic" / "main.jsonl").read_text().splitlines()
    history = [_renamed(line, "old") for line in source]
    target.write_text("\n".join(history) + "\n")

    agent_id, incarnation = await make_agent(aops_pool)
    async with aops_pool.connection() as conn:
        await conn.execute(
            "INSERT INTO agent_harness_messages (agent_id, seq, harness, item) "
            "VALUES (%s, 0, 'claude_code', %s::jsonb)",
            (
                agent_id,
                json.dumps(
                    {"item_id": "h0.0", "kind": "inbound_chat", "payload": "parent's history"}
                ),
            ),
        )
    row = await make_inbound_row(aops_pool, agent_id)
    committed: list[Committed] = []
    writer = make_writer(
        aops_pool,
        agent_id,
        incarnation,
        ClaudeRecordReader(now_ns=now_ns, claude_home=tmp_path, session_id=session),
        committed,
        session=session,
    )
    assert not await writer.read_now(), "nothing of the history is projected"
    assert len(await timeline_of(aops_pool, agent_id)) == 1

    send_input(writer, BASIC, "k1", row)
    with target.open("a") as handle:
        handle.write("\n".join(source) + "\n")
    assert await writer.read_now()
    kinds = [kind for kind, _, _ in await timeline_of(aops_pool, agent_id)]
    assert kinds == [
        "inbound_chat",
        "inbound_chat",
        "agent_reasoning",
        "agent_code",
        "code_output",
        "agent_reasoning",
        "agent_chat",
    ]
    assert await status_of(aops_pool, row) == "done"
    async with aops_pool.connection() as conn:
        cursor = await conn.execute(
            "SELECT count(*) FROM agent_harness_transcript_rows WHERE agent_id = %s", (agent_id,)
        )
        stored = await cursor.fetchone()
    assert stored is not None and int(stored[0]) < len(history)


def _renamed(line: str, tag: str) -> str:
    row = json.loads(line)
    if "uuid" in row:
        row["uuid"] = f"{tag}-{row['uuid']}"
    message = row.get("message")
    if isinstance(message, dict) and "id" in message:
        message["id"] = f"{tag}-{message['id']}"
    return json.dumps(row)


async def test_a_fresh_agent_with_no_timeline_reads_the_whole_record(
    aops_pool: AsyncConnectionPool, tmp_path: Path
) -> None:
    session = rows("basic")[0]["sessionId"]
    assert isinstance(session, str)
    folder = tmp_path / "projects" / "p"
    folder.mkdir(parents=True)
    _ = shutil.copyfile(FIXTURES / "basic" / "main.jsonl", folder / f"{session}.jsonl")
    agent_id, incarnation = await make_agent(aops_pool)
    row = await make_inbound_row(aops_pool, agent_id)
    writer = make_writer(
        aops_pool,
        agent_id,
        incarnation,
        ClaudeRecordReader(now_ns=now_ns, claude_home=tmp_path, session_id=session),
        [],
        session=session,
    )
    send_input(writer, BASIC, "k1", row)
    assert await writer.read_now()
    assert len(await timeline_of(aops_pool, agent_id)) == 6


# meiva:note
# A turn that goes on for a very long time with a call open has no quiet point, so a restart would replay every row
# since the last one. The replay reads at most `REBUILD_MAX_ROWS` rows (the latest ones) and says so; a call that
# began before them is not known, and the output it later produces is held and not shown.
async def test_a_restart_in_a_turn_without_a_quiet_point_rebuilds_from_a_bounded_tail(
    aops_pool: AsyncConnectionPool,
) -> None:
    agent_id, incarnation = await make_agent(aops_pool)
    entries = entries_of("basic")
    reader = ScriptedReader({"main": entries})
    total = 40
    async with aops_pool.connection() as conn:
        await aappend_transcript_rows(
            conn,
            agent_id,
            "session-1",
            [("main", n, {"entry": n % len(entries)}) for n in range(total)],
        )
        await aset_transcript_position(
            conn, agent_id, "session-1", StoredPosition("main", "0", -1), None
        )
    reader.records_data["main"] = [entries[n % len(entries)] for n in range(total)]
    spy = _SpyReader(reader.records_data)
    reader = spy
    writer = HarnessTimelineWriter(
        pool=aops_pool,
        agent_id=agent_id,
        harness="claude_code",
        session_id="session-1",
        marker_id=lambda: "m",
        incarnation=incarnation,
        reader=reader,
        policy=ReplyPolicy(explicit_visibility=False, delivery_mode=None),
        render_tool=_render_name,
        after_commit=_ignore,
        cli_version=lambda: None,
        clock=system_clock(),
        rebuild_max_rows=10,
    )
    writer.start_reading()
    _ = await writer.read_now()
    assert spy.seen == list(range(total - 10, total))

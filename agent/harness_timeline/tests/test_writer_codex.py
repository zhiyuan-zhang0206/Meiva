from __future__ import annotations

import copy
from pathlib import Path

from psycopg_pool import AsyncConnectionPool

from agent.harness_timeline.tests.support import (
    make_agent,
    make_inbound_row,
    make_writer,
    send_input,
    status_of,
    timeline_of,
)
from agent.harness_timeline.writer import Committed, HarnessTimelineWriter
from base.lm.harness.codex.transcript.shapes import Obj
from base.lm.harness.codex.transcript.tests.rig import STAMP, THREAD, Rig, basic
from base.lm.harness.codex.transcript.tests.support import (
    load_json,
    row_item,
    thread_turns,
    turn_items,
)

# meiva:note
# The writer on a Codex thread, end to end over the real reader and a real database: what a turn writes, how an input
# is proven delivered (the client id we sent comes back in the record, once), what a steer the interrupt swallowed
# does (it is never in the record, so it stays undelivered and the graph sends it again), and that a restarted writer
# continues from its stored position and rebuilds what it needs from the stored rows alone. The history is the fake
# app-server's, the rollout a real file the reader tails.
# meiva:link agent/harness_timeline/README.md


async def _drain(writer: HarnessTimelineWriter) -> None:
    for _ in range(50):
        if not await writer.read_now():
            return
    raise AssertionError("the writer never ran out of things to read")


def _aborted(turn: str, ordinal: int) -> Obj:
    return {
        "type": "event_msg",
        "timestamp": STAMP,
        "ordinal": ordinal,
        "payload": {"type": "turn_aborted", "turn_id": turn, "reason": "interrupted"},
    }


async def test_a_codex_turn_is_written_once_in_order_and_settles_its_input_by_the_client_id(
    aops_pool: AsyncConnectionPool, tmp_path: Path
) -> None:
    agent_id, incarnation = await make_agent(aops_pool)
    row = await make_inbound_row(aops_pool, agent_id)
    rig = Rig(tmp_path)
    committed: list[Committed] = []
    writer = make_writer(aops_pool, agent_id, incarnation, rig.reader, committed, session=THREAD)
    send_input(writer, "cid-s1-a", "cid-s1-a", row)
    rows, lines = basic()
    rig.feed(rows[:6], lines[:25])
    await _drain(writer)
    kinds = [kind for kind, _, _ in await timeline_of(aops_pool, agent_id)]
    assert kinds == [
        "inbound_chat",
        "agent_chat",
        "agent_code",
        "code_output",
        "agent_chat",
    ]
    assert await status_of(aops_pool, row) == "done"
    assert writer.closed_turns == 1
    assert [s.input_key for c in committed for s in c.plan.settlements] == ["cid-s1-a"]
    usage = [spend for c in committed for spend in c.plan.usage]
    assert len(usage) == 2 and all(spend.record == "main" for spend in usage)
    assert not [report for c in committed for report in c.plan.anomalies]
    async with aops_pool.connection() as conn:
        cursor = await conn.execute(
            "SELECT count(*), count(DISTINCT source_key), count(source_key) "
            "FROM agent_harness_messages WHERE agent_id = %s",
            (agent_id,),
        )
        assert await cursor.fetchone() == (5, 5, 5)
        cursor = await conn.execute(
            "SELECT count(*) FROM agent_harness_transcript_rows WHERE agent_id = %s", (agent_id,)
        )
        stored = await cursor.fetchone()
        assert stored is not None and 8 <= int(str(stored[0])) <= 14
    assert not await writer.read_now(), "a second read finds nothing and writes nothing"
    assert len(await timeline_of(aops_pool, agent_id)) == 5


async def test_a_steer_the_interrupt_swallowed_is_never_proven_and_stays_to_be_sent_again(
    aops_pool: AsyncConnectionPool, tmp_path: Path
) -> None:
    agent_id, incarnation = await make_agent(aops_pool)
    first = await make_inbound_row(aops_pool, agent_id)
    second = await make_inbound_row(aops_pool, agent_id)
    steer = await make_inbound_row(aops_pool, agent_id)
    rig = Rig(tmp_path)
    committed: list[Committed] = []
    writer = make_writer(aops_pool, agent_id, incarnation, rig.reader, committed, session=THREAD)
    for wire, row in (("cid-s3-a", first), ("cid-s3-b", second), ("cid-s3-steer", steer)):
        send_input(writer, wire, wire, row)
    turns = thread_turns(load_json("interrupt/thread.json"))
    rows: list[Obj] = [
        {"turnId": turn["id"], "item": item, "startedAtMs": 1, "completedAtMs": 2}
        for turn in turns
        for item in turn_items(turn)
    ]
    rig.feed(rows, [_aborted(str(turn["id"]), 2_000 + n) for n, turn in enumerate(turns)])
    await _drain(writer)
    assert [await status_of(aops_pool, r) for r in (first, second, steer)] == [
        "done",
        "done",
        "claimed",
    ]
    assert [sent.key for sent in writer.undelivered()] == ["cid-s3-steer"]
    kinds = [kind for kind, _, _ in await timeline_of(aops_pool, agent_id)]
    assert kinds.count("inbound_chat") == 2
    markers = [
        c for commit in committed for c in commit.plan.items if c.item.kind == "system_marker"
    ]
    assert [m.item.payload for m in markers] == ["status:Turn interrupted"] * 2


async def test_a_restarted_writer_continues_from_its_position_and_a_steer_still_joins_its_turn(
    aops_pool: AsyncConnectionPool, tmp_path: Path
) -> None:
    agent_id, incarnation = await make_agent(aops_pool)
    first = await make_inbound_row(aops_pool, agent_id)
    steer = await make_inbound_row(aops_pool, agent_id)
    rig = Rig(tmp_path)
    committed: list[Committed] = []
    writer = make_writer(aops_pool, agent_id, incarnation, rig.reader, committed, session=THREAD)
    send_input(writer, "cid-s1-a", "cid-s1-a", first)
    rows, lines = basic()
    rig.feed(rows[:3], lines[:12])
    await _drain(writer)
    assert len(await timeline_of(aops_pool, agent_id)) == 2
    again = make_writer(
        aops_pool, agent_id, incarnation, rig.restarted_reader(), committed, session=THREAD
    )
    steer_row = copy.deepcopy(rows[0])
    row_item(steer_row)["id"], row_item(steer_row)["clientId"] = "u-steer", "cid-steer"
    send_input(again, "cid-steer", "cid-steer", steer)
    rig.feed([steer_row], [])
    await _drain(again)
    kinds = [kind for kind, _, _ in await timeline_of(aops_pool, agent_id)]
    assert kinds == ["inbound_chat", "agent_chat", "inbound_chat"]
    assert await status_of(aops_pool, steer) == "done"
    assert not await again.read_now()
    assert len(await timeline_of(aops_pool, agent_id)) == 3

# meiva:note
# The writer's correctness under concurrency and across its life, each pinned to a failure it once had: two callers
# reading at once commit a plan once and account for it once; a sub-agent record that appears after the start is read
# from its first row; a rebuild that fails is made again; a held item is released and reported; a synthetic input whose
# id is in the history does not move where a session with history starts; a session that is not known yet reads nothing.
# meiva:link agent/harness_timeline/README.md

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest
from psycopg_pool import AsyncConnectionPool

from agent.harness_timeline.tests.support import (
    BASIC,
    ScriptedReader,
    make_agent,
    make_inbound_row,
    make_writer,
    send_input,
    status_of,
    timeline_of,
)
from agent.harness_timeline.writer import HOLD_SECONDS, Committed
from base.agents.history.harness_messages import aappend_keyed_timeline_items
from base.agents.history.timeline_item import TimelineItem
from base.clock.tests.fakes import clock_over
from base.clock.tests.manual_timers import ManualTimers
from base.lm.harness.claude.transcript.tests.support import entries_of
from base.lm.harness.session.transcript.drafts import InboundPart, SentInput
from base.lm.harness.session.transcript.entries import (
    AgentText,
    AnomalyKind,
    AnyEntry,
    HumanInput,
    Order,
    SubagentLink,
    ToolCall,
    TurnEnd,
)
from base.native_process.runtime_incarnation import RuntimeIncarnation


async def _history(pool: AsyncConnectionPool, agent_id: int) -> None:
    old = TimelineItem(item_id="x", kind="agent_chat", payload="an earlier reply")
    async with pool.connection() as conn:
        _ = await aappend_keyed_timeline_items(
            conn, agent_id, "claude_code", [("old:1", old)], projection_version=1
        )


def _old_turn() -> list[AnyEntry]:
    return [
        HumanInput(key="h-1", order=Order(0), turn="old", wire_id="old-wire", text="old"),
        AgentText(key="h-2", order=Order(1), turn="old", text="old reply", message_ref="m0"),
        TurnEnd(key="h-3", order=Order(2), turn="old"),
    ]


async def test_readers_at_once_commit_a_plan_once_and_account_for_it_once(
    aops_pool: AsyncConnectionPool,
) -> None:
    agent_id, incarnation = await make_agent(aops_pool)
    row = await make_inbound_row(aops_pool, agent_id)
    reader = ScriptedReader({"main": entries_of("basic")})
    reader.visible["main"] = len(reader.records_data["main"])
    committed: list[Committed] = []
    writer = make_writer(aops_pool, agent_id, incarnation, reader, committed)
    send_input(writer, BASIC, "k1", row)

    _ = await asyncio.gather(*(writer.read_now() for _ in range(6)))

    assert len(committed) == 1, "one plan, one set of effects"
    assert writer.closed_turns == 1, "one closing, counted once"
    assert len(await timeline_of(aops_pool, agent_id)) == 6
    assert await status_of(aops_pool, row) == "done"


async def test_a_sub_agent_that_appears_after_the_start_is_read_from_its_first_row(
    aops_pool: AsyncConnectionPool,
) -> None:
    agent_id, incarnation = await make_agent(aops_pool)
    await _history(aops_pool, agent_id)
    live: list[AnyEntry] = [
        ToolCall(key="c-1", order=Order(3), call_id="call-1", name="Task", tool_input={}),
        SubagentLink(key="l-1", order=Order(4), call_id="call-1", child_record="subX"),
    ]
    sub: list[AnyEntry] = [
        AgentText(
            key="s-1", order=Order(0), record="subX", text="the sub-agent says", message_ref="sm"
        )
    ]
    reader = ScriptedReader({"main": [*_old_turn(), *live], "subX": sub}, listed=["main"])
    reader.visible["main"] = 3
    writer = make_writer(aops_pool, agent_id, incarnation, reader, [])
    assert not await writer.read_now(), "the history is skipped, nothing new yet"

    reader.visible["main"] = 5
    reader.appear("subX")
    reader.visible["subX"] = 1
    while await writer.read_now():
        pass

    shown = await timeline_of(aops_pool, agent_id)
    kinds = [kind for kind, _, _ in shown]
    assert kinds == ["agent_chat", "agent_code", "subagent_text"], shown
    assert shown[2][2] == shown[1][1], "nested under the call that started it"


async def test_a_rebuild_that_fails_is_made_again_before_anything_is_read_on_top_of_it(
    aops_pool: AsyncConnectionPool,
) -> None:
    agent_id, incarnation = await make_agent(aops_pool)
    row = await make_inbound_row(aops_pool, agent_id)
    entries = entries_of("basic")
    reader = ScriptedReader({"main": entries})
    reader.visible["main"] = 5
    first = make_writer(aops_pool, agent_id, incarnation, reader, [])
    send_input(first, BASIC, "k1", row)
    assert await first.read_now()

    second = make_writer(aops_pool, agent_id, incarnation, reader, [])
    send_input(second, BASIC, "k1", row)
    reader.fail_parse_stored = True
    with pytest.raises(OSError):
        _ = await second.read_now()
    reader.visible["main"] = len(entries)
    assert await second.read_now()

    assert second.undelivered() == (), (
        "the stored rows were rebuilt, so the input is known delivered"
    )
    kinds = [kind for kind, _, _ in await timeline_of(aops_pool, agent_id)]
    assert kinds.count("inbound_chat") == 1 and kinds[-1] == "agent_chat"


async def test_what_is_held_for_a_link_is_released_top_level_and_reported_after_the_bound(
    aops_pool: AsyncConnectionPool,
) -> None:
    agent_id, incarnation = await make_agent(aops_pool)
    timers = ManualTimers()
    lonely: list[AnyEntry] = [
        AgentText(key="s-1", order=Order(0), record="subY", text="nobody links me", message_ref="m")
    ]
    reader = ScriptedReader({"main": [], "subY": lonely})
    reader.visible["subY"] = 1
    committed: list[Committed] = []
    writer = make_writer(
        aops_pool, agent_id, incarnation, reader, committed, clock=clock_over(timers)
    )
    assert await writer.read_now(), "the read itself is a commit (its position)"
    assert await timeline_of(aops_pool, agent_id) == [], "held, not shown yet"

    timers.advance(HOLD_SECONDS + 1)
    while await writer.read_now():
        pass

    shown = await timeline_of(aops_pool, agent_id)
    assert [(kind, parent) for kind, _, parent in shown] == [("subagent_text", None)]
    anomalies = [a for c in committed for a in c.plan.anomalies]
    assert [(a.anomaly, a.detail) for a in anomalies] == [(AnomalyKind.UNLINKED_RELEASED, "subY")]


async def test_a_synthetic_input_whose_id_is_in_the_history_does_not_move_the_start(
    aops_pool: AsyncConnectionPool,
) -> None:
    agent_id, incarnation = await make_agent(aops_pool)
    await _history(aops_pool, agent_id)
    row = await make_inbound_row(aops_pool, agent_id)
    entries: list[AnyEntry] = [
        HumanInput(key="a-1", order=Order(0), turn="old", wire_id="fixed", text="correction"),
        AgentText(key="a-2", order=Order(1), turn="old", text="old reply", message_ref="m0"),
        TurnEnd(key="a-3", order=Order(2), turn="old"),
        HumanInput(key="a-4", order=Order(3), turn="new", wire_id="real-wire", text="hello"),
        AgentText(key="a-5", order=Order(4), turn="new", text="the new reply", message_ref="m1"),
    ]
    reader = ScriptedReader({"main": entries})
    reader.visible["main"] = 5
    writer = make_writer(aops_pool, agent_id, incarnation, reader, [])
    writer.register_sent(
        SentInput(key="inbound-turn-1:visibility-correction", wire_id="fixed", text="c", parts=()),
        items={},
        inbound_ids=(),
    )
    send_input(writer, "real-wire", "k1", row)

    while await writer.read_now():
        pass

    kinds = [kind for kind, _, _ in await timeline_of(aops_pool, agent_id)]
    assert kinds == ["agent_chat", "inbound_chat", "agent_chat"], "only the new turn was projected"


async def test_a_session_that_is_not_known_yet_reads_nothing_and_shows_the_input_undelivered(
    aops_pool: AsyncConnectionPool,
) -> None:
    agent_id, incarnation = await make_agent(aops_pool)
    row = await make_inbound_row(aops_pool, agent_id)
    reader = ScriptedReader({"main": entries_of("basic")})
    reader.visible["main"] = 6
    known: list[str | None] = [None]
    writer = make_writer(aops_pool, agent_id, incarnation, reader, [], session=lambda: known[0])
    send_input(writer, BASIC, "k1", row)
    assert not await writer.read_now()
    assert [sent.key for sent in writer.undelivered()] == ["k1"]
    known[0] = "session-1"
    assert await writer.read_now()
    assert writer.undelivered() == ()


async def test_one_input_that_carried_several_rows_settles_all_of_them_in_one_commit(
    aops_pool: AsyncConnectionPool,
) -> None:
    agent_id, incarnation = await make_agent(aops_pool)
    first = await make_inbound_row(aops_pool, agent_id)
    second = await make_inbound_row(aops_pool, agent_id, status="pending")
    reader = ScriptedReader({"main": entries_of("basic")})
    reader.visible["main"] = 6
    writer = make_writer(aops_pool, agent_id, incarnation, reader, [])
    item = TimelineItem(item_id="x", kind="inbound_chat", payload="hello", inbound_id=first)
    writer.register_sent(
        SentInput(key="k1", wire_id=BASIC, text="hello", parts=(InboundPart("p1", "hello"),)),
        items={"p1": item},
        inbound_ids=(first, second),
    )

    assert await writer.read_now()

    assert [await status_of(aops_pool, first), await status_of(aops_pool, second)] == [
        "done",
        "done",
    ]


async def test_a_fenced_writer_that_is_bound_a_valid_incarnation_commits_once_and_runs_its_effects_once(
    aops_pool: AsyncConnectionPool,
) -> None:
    agent_id, incarnation = await make_agent(aops_pool)
    row = await make_inbound_row(aops_pool, agent_id)
    reader = ScriptedReader({"main": entries_of("basic")})
    reader.visible["main"] = len(reader.records_data["main"])
    committed: list[Committed] = []
    stale = RuntimeIncarnation(agent_id, uuid4(), uuid4())
    writer = make_writer(aops_pool, agent_id, stale, reader, committed)
    send_input(writer, BASIC, "k1", row)

    assert not await writer.read_now() and writer.fenced
    assert not await writer.read_now(), "fenced, it keeps its plan and writes nothing"
    assert committed == [] and await timeline_of(aops_pool, agent_id) == []

    writer.bind_incarnation(incarnation)
    assert await writer.read_now()
    assert not await writer.read_now()

    assert len(committed) == 1 and writer.closed_turns == 1
    assert await status_of(aops_pool, row) == "done"

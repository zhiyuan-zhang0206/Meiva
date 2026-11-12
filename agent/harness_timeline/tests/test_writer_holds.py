# meiva:note
# (The tests that build a link that comes late describe the foreground form of a sub-agent; in the SDK's non-interactive
# mode the CLI starts sub-agents in the background by default and the link comes with the launch.)
# What the writer holds back, and for how long. A result whose call has not been read is on a clock of its own per call and is
# shown top-level when its own `HOLD_SECONDS` have passed, not when another's have. The output of a sub-agent whose link has
# not been read is on no clock: a foreground sub-agent's link comes with the result of the call that started it, however long
# it runs, so nothing that is only slow is released (and then nested half on top and half under the call); it is released
# only when the writer closes for good or the session is replaced, never from what has been read of the main record. A call abandoned with its turn is still known
# after a restart when its result comes, within a bounded number of rows.
# meiva:link agent/harness_timeline/README.md

from __future__ import annotations

from typing import cast

import pytest
from psycopg import OperationalError
from psycopg_pool import AsyncConnectionPool

from agent.harness_timeline.holds import HOLD_SECONDS
from agent.harness_timeline.tests.support import (
    ScriptedReader,
    make_agent,
    make_writer,
    timeline_of,
)
from agent.harness_timeline.writer import Committed, HarnessTimelineWriter
from base.clock.tests.fakes import clock_over
from base.clock.tests.manual_timers import ManualTimers
from base.lm.harness.session.transcript.entries import (
    AgentText,
    AnomalyKind,
    AnyEntry,
    HumanInput,
    Order,
    SubagentLink,
    ToolCall,
    ToolResult,
    TurnEnd,
)


def _sub(record: str, n: int) -> AnyEntry:
    return AgentText(
        key=f"{record}-{n}",
        order=Order(n),
        record=record,
        text=f"{record} {n}",
        message_ref=f"{record}m{n}",
    )


def _task(index: int) -> list[AnyEntry]:
    return [
        ToolCall(
            key=f"c{index}",
            order=Order(10 * index + 1),
            turn="t0",
            call_id=f"call{index}",
            name="Task",
            tool_input={},
        )
    ]


def _result(index: int) -> list[AnyEntry]:
    return [
        ToolResult(
            key=f"r{index}",
            order=Order(10 * index + 2),
            turn="t0",
            call_id=f"call{index}",
            content="done",
        ),
        SubagentLink(
            key=f"l{index}",
            order=Order(10 * index + 3),
            turn="t0",
            call_id=f"call{index}",
            child_record=f"sub{index}",
        ),
    ]


_OPEN: list[AnyEntry] = [HumanInput(key="i0", order=Order(0), turn="t0", wire_id="w", text="go")]
_CLOSE: list[AnyEntry] = [TurnEnd(key="e0", order=Order(900), turn="t0")]


async def test_a_foreground_sub_agent_that_runs_longer_than_the_hold_time_is_not_released_for_it(
    aops_pool: AsyncConnectionPool,
) -> None:
    agent_id, incarnation = await make_agent(aops_pool)
    timers = ManualTimers()
    main: list[AnyEntry] = [*_OPEN, *_task(1), *_task(2)]
    reader = ScriptedReader(
        {"main": main, "sub1": [_sub("sub1", n) for n in range(4)], "sub2": [_sub("sub2", 0)]}
    )
    reader.visible.update({"main": 3, "sub1": 2, "sub2": 0})
    committed: list[Committed] = []
    writer = make_writer(
        aops_pool, agent_id, incarnation, reader, committed, clock=clock_over(timers)
    )
    while await writer.read_now():
        pass
    timers.advance(HOLD_SECONDS * 3)
    reader.visible["sub2"] = 1
    while await writer.read_now():
        pass
    assert [kind for kind, _, _ in await timeline_of(aops_pool, agent_id)] == [
        "inbound_chat",
        "agent_code",
        "agent_code",
    ], "two sub-agents are running, one for longer than the hold time: nothing of them is shown yet"

    main.extend(_result(1))
    reader.visible.update({"main": len(main), "sub1": 4})
    while await writer.read_now():
        pass
    shown = await timeline_of(aops_pool, agent_id)
    calls = {item_id: n for n, (kind, item_id, _) in enumerate(shown) if kind == "agent_code"}
    nested = [(kind, parent) for kind, _, parent in shown if kind == "subagent_text"]
    assert len(nested) == 4 and {parent for _, parent in nested} == {next(iter(calls))}, (
        "all of the first sub-agent's output is under its own call: none of it was shown on top"
    )
    assert [a for c in committed for a in c.plan.anomalies] == []


async def test_a_result_without_its_call_is_released_on_its_own_clock_not_another_calls(
    aops_pool: AsyncConnectionPool,
) -> None:
    agent_id, incarnation = await make_agent(aops_pool)
    timers = ManualTimers()
    first = ToolResult(key="ra", order=Order(1), turn="t0", call_id="gone-a", content="a")
    second = ToolResult(key="rb", order=Order(2), turn="t0", call_id="gone-b", content="b")
    reader = ScriptedReader({"main": [*_OPEN, first, second]})
    reader.visible["main"] = 2
    writer = make_writer(aops_pool, agent_id, incarnation, reader, [], clock=clock_over(timers))
    while await writer.read_now():
        pass
    timers.advance(HOLD_SECONDS - 20)
    reader.visible["main"] = 3
    while await writer.read_now():
        pass
    timers.advance(30)
    while await writer.read_now():
        pass
    outputs = [kind for kind, _, _ in await timeline_of(aops_pool, agent_id)]
    assert outputs.count("code_output") == 1, (
        "the first result's clock ran out, the second's did not"
    )
    timers.advance(HOLD_SECONDS)
    while await writer.read_now():
        pass
    assert [k for k, _, _ in await timeline_of(aops_pool, agent_id)].count("code_output") == 2


async def test_a_result_that_comes_after_a_restart_for_a_call_whose_turn_closed_is_still_paired(
    aops_pool: AsyncConnectionPool,
) -> None:
    agent_id, incarnation = await make_agent(aops_pool)
    entries: list[AnyEntry] = [
        *_OPEN,
        ToolCall(key="c", order=Order(1), turn="t0", call_id="slow", name="Bash", tool_input={}),
        TurnEnd(key="e0", order=Order(2), turn="t0"),
        HumanInput(key="i1", order=Order(3), turn="t1", wire_id="w1", text="next"),
        AgentText(key="a1", order=Order(4), turn="t1", text="ok", message_ref="m1"),
        TurnEnd(key="e1", order=Order(5), turn="t1"),
        ToolResult(key="late", order=Order(6), turn="t0", call_id="slow", content="finished"),
    ]
    reader = ScriptedReader({"main": entries})
    reader.visible["main"] = 6
    first = make_writer(aops_pool, agent_id, incarnation, reader, [])
    while await first.read_now():
        pass

    second = make_writer(aops_pool, agent_id, incarnation, reader, [])
    reader.visible["main"] = 7
    while await second.read_now():
        pass
    shown = await timeline_of(aops_pool, agent_id)
    assert [k for k, _, _ in shown].count("code_output") == 1
    assert second.projector_sizes()["orphans"] == 0, "the call was known again after the restart"


async def _read_all(writer: HarnessTimelineWriter) -> None:
    while await writer.read_now():
        pass


def _running_foreground_sub_agent() -> tuple[list[AnyEntry], list[AnyEntry]]:
    main: list[AnyEntry] = [
        HumanInput(key="i0", order=Order(0), turn="t0", wire_id="w0", text="old"),
        ToolCall(key="old", order=Order(1), turn="t0", call_id="old", name="Bash", tool_input={}),
        TurnEnd(key="e0", order=Order(2), turn="t0"),
        HumanInput(key="i1", order=Order(3), turn="t1", wire_id="w1", text="go"),
        ToolCall(key="c1", order=Order(4), turn="t1", call_id="call1", name="Task", tool_input={}),
    ]
    return main, [_sub("a1sub", n) for n in range(3)]


async def test_what_a_restart_replays_does_not_depend_on_which_record_is_replayed_first(
    aops_pool: AsyncConnectionPool,
) -> None:
    agent_id, incarnation = await make_agent(aops_pool)
    main, sub = _running_foreground_sub_agent()
    reader = ScriptedReader({"main": main, "a1sub": sub})
    reader.visible.update({"main": 5, "a1sub": 3})
    first = make_writer(aops_pool, agent_id, incarnation, reader, [])
    await _read_all(first)

    committed: list[Committed] = []
    second = make_writer(aops_pool, agent_id, incarnation, reader, committed)
    await _read_all(second)
    assert second.projector_sizes()["held"] == 1, (
        "the running sub-agent's output is held again after the replay"
    )
    main.extend(
        [
            ToolResult(key="r1", order=Order(5), turn="t1", call_id="call1", content="done"),
            SubagentLink(
                key="l1", order=Order(6), turn="t1", call_id="call1", child_record="a1sub"
            ),
            TurnEnd(key="e1", order=Order(7), turn="t1"),
        ]
    )
    reader.visible["main"] = len(main)
    await _read_all(second)

    shown = await timeline_of(aops_pool, agent_id)
    calls = [item_id for kind, item_id, _ in shown if kind == "agent_code"]
    nested = [parent for kind, _, parent in shown if kind == "subagent_text"]
    assert len(nested) == 3 and set(nested) == {calls[1]}, shown
    assert [a for c in committed for a in c.plan.anomalies] == []


async def test_the_oldest_abandoned_call_beyond_the_window_does_not_take_the_newer_ones_with_it(
    aops_pool: AsyncConnectionPool,
) -> None:
    agent_id, incarnation = await make_agent(aops_pool)
    filler = [
        AgentText(key=f"f{n}", order=Order(n), turn="t1", text="x", message_ref=f"fm{n}")
        for n in range(4, 5503)
    ]
    entries: list[AnyEntry] = [
        HumanInput(key="i0", order=Order(0), turn="t0", wire_id="w0", text="a"),
        ToolCall(key="a", order=Order(1), turn="t0", call_id="never", name="Bash", tool_input={}),
        TurnEnd(key="e0", order=Order(2), turn="t0"),
        HumanInput(key="i1", order=Order(3), turn="t1", wire_id="w1", text="b"),
        *filler,
        ToolCall(key="b", order=Order(5503), turn="t1", call_id="slow", name="Bash", tool_input={}),
        TurnEnd(key="e1", order=Order(5504), turn="t1"),
        ToolResult(key="late", order=Order(5505), turn="t1", call_id="slow", content="done"),
    ]
    reader = ScriptedReader({"main": entries})
    reader.visible["main"] = 5505
    first = make_writer(aops_pool, agent_id, incarnation, reader, [])
    await _read_all(first)
    async with aops_pool.connection() as conn:
        cursor = await conn.execute(
            "SELECT rebuild_ordinal FROM agent_harness_transcript_positions WHERE agent_id = %s",
            (agent_id,),
        )
        row = await cursor.fetchone()
    assert row is not None and row[0] == 5502, (
        "the restart point is before the call within the window, not at the end"
    )

    second = make_writer(aops_pool, agent_id, incarnation, reader, [])
    reader.visible["main"] = 5506
    await _read_all(second)
    assert second.projector_sizes()["orphans"] == 0, "the late result found its call"


async def test_a_writer_that_closes_shows_what_it_held_and_a_restart_does_not_repeat_it(
    aops_pool: AsyncConnectionPool,
) -> None:
    agent_id, incarnation = await make_agent(aops_pool)
    main, sub = _running_foreground_sub_agent()
    reader = ScriptedReader({"main": main, "a1sub": sub})
    reader.visible.update({"main": 5, "a1sub": 3})
    committed: list[Committed] = []
    writer = make_writer(aops_pool, agent_id, incarnation, reader, committed)
    await _read_all(writer)
    assert [k for k, _, _ in await timeline_of(aops_pool, agent_id)].count("subagent_text") == 0

    await writer.release_held()
    assert [
        (k, p) for k, _, p in await timeline_of(aops_pool, agent_id) if k == "subagent_text"
    ] == [("subagent_text", None)] * 3
    assert [(a.anomaly, a.detail) for c in committed for a in c.plan.anomalies] == [
        (AnomalyKind.UNLINKED_RELEASED, "a1sub")
    ]

    main.extend(
        [
            ToolResult(key="r1", order=Order(5), turn="t1", call_id="call1", content="done"),
            SubagentLink(
                key="l1", order=Order(6), turn="t1", call_id="call1", child_record="a1sub"
            ),
        ]
    )
    reader.visible["main"] = len(main)
    again: list[Committed] = []
    restarted = make_writer(aops_pool, agent_id, incarnation, reader, again)
    await _read_all(restarted)
    kinds = [k for k, _, _ in await timeline_of(aops_pool, agent_id)]
    assert kinds.count("subagent_text") == 3, "keyed: shown once, not again under the call"
    assert [a for c in again for a in c.plan.anomalies] == []


async def test_what_the_old_session_held_is_shown_when_the_session_is_replaced_by_one_with_another_id(
    aops_pool: AsyncConnectionPool,
) -> None:
    agent_id, incarnation = await make_agent(aops_pool)
    main, sub = _running_foreground_sub_agent()
    reader = ScriptedReader({"main": main, "a1sub": sub})
    reader.visible.update({"main": 5, "a1sub": 3})
    session = ["session-1"]
    writer = make_writer(aops_pool, agent_id, incarnation, reader, [], session=lambda: session[0])
    await _read_all(writer)
    assert [k for k, _, _ in await timeline_of(aops_pool, agent_id)].count("subagent_text") == 0
    session[0] = "session-2"
    assert await writer.read_now(), (
        "the old session's holds are committed before the new one starts"
    )
    assert [k for k, _, _ in await timeline_of(aops_pool, agent_id)].count("subagent_text") == 3


async def test_a_result_a_replay_finds_held_is_shown_when_its_own_time_is_up_without_a_new_batch(
    aops_pool: AsyncConnectionPool,
) -> None:
    agent_id, incarnation = await make_agent(aops_pool)
    entries: list[AnyEntry] = [
        *_OPEN,
        ToolResult(key="rg", order=Order(1), turn="t0", call_id="gone", content="g"),
    ]
    reader = ScriptedReader({"main": entries})
    reader.visible["main"] = 2
    first = make_writer(aops_pool, agent_id, incarnation, reader, [])
    await _read_all(first)

    timers = ManualTimers()
    second = make_writer(aops_pool, agent_id, incarnation, reader, [], clock=clock_over(timers))
    assert not await second.read_now(), "replayed, nothing new, and not yet time"
    timers.advance(HOLD_SECONDS + 1)
    assert await second.read_now(), "no new batch is needed for its time to be up"
    assert [k for k, _, _ in await timeline_of(aops_pool, agent_id)].count("code_output") == 1


class _FlakyPool:
    # meiva:note
    # A pool of the test database that refuses to lend a connection while armed, once: what a transient database error
    # looks like to the writer's transaction.
    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool
        self.fail_next = False

    def connection(self, timeout: float | None = None) -> object:
        if self.fail_next:
            self.fail_next = False
            raise OperationalError("the connection could not be taken")
        return self._pool.connection(timeout=timeout)


async def test_a_sub_agent_file_that_is_ahead_of_main_is_not_released_for_a_turn_main_has_closed(
    aops_pool: AsyncConnectionPool,
) -> None:
    agent_id, incarnation = await make_agent(aops_pool)
    main: list[AnyEntry] = [
        HumanInput(key="i0", order=Order(0), turn="t0", wire_id="w0", text="first"),
        AgentText(key="a0", order=Order(1), turn="t0", text="done", message_ref="m0"),
        TurnEnd(key="e0", order=Order(2), turn="t0"),
    ]
    reader = ScriptedReader({"main": main, "a1sub": [_sub("a1sub", n) for n in range(3)]})
    reader.visible.update({"main": 3, "a1sub": 3})
    committed: list[Committed] = []
    writer = make_writer(aops_pool, agent_id, incarnation, reader, committed)
    await _read_all(writer)
    shown = [k for k, _, _ in await timeline_of(aops_pool, agent_id)]
    assert "subagent_text" not in shown, (
        "main closed a turn, but the sub-agent belongs to the one it has not read yet"
    )

    main.extend(
        [
            HumanInput(key="i1", order=Order(3), turn="t1", wire_id="w1", text="second"),
            ToolCall(
                key="c1", order=Order(4), turn="t1", call_id="call1", name="Task", tool_input={}
            ),
            ToolResult(key="r1", order=Order(5), turn="t1", call_id="call1", content="done"),
            SubagentLink(
                key="l1", order=Order(6), turn="t1", call_id="call1", child_record="a1sub"
            ),
        ]
    )
    reader.visible["main"] = len(main)
    await _read_all(writer)
    rows = await timeline_of(aops_pool, agent_id)
    call = next(item_id for kind, item_id, _ in rows if kind == "agent_code")
    assert [p for k, _, p in rows if k == "subagent_text"] == [call] * 3
    assert [a for c in committed for a in c.plan.anomalies] == []


async def test_what_a_replaced_session_held_is_not_lost_when_the_first_commit_fails(
    aops_pool: AsyncConnectionPool,
) -> None:
    agent_id, incarnation = await make_agent(aops_pool)
    main, sub = _running_foreground_sub_agent()
    reader = ScriptedReader({"main": main, "a1sub": sub})
    reader.visible.update({"main": 5, "a1sub": 3})
    session = ["session-1"]
    pool = _FlakyPool(aops_pool)
    writer = make_writer(
        cast("AsyncConnectionPool", pool),
        agent_id,
        incarnation,
        reader,
        [],
        session=lambda: session[0],
    )
    await _read_all(writer)
    session[0] = "session-2"
    pool.fail_next = True
    with pytest.raises(OperationalError):
        _ = await writer.read_now()
    assert await writer.read_now(), (
        "the plan that releases what was held is kept and committed again"
    )
    assert [k for k, _, _ in await timeline_of(aops_pool, agent_id)].count("subagent_text") == 3


async def test_a_closing_writer_commits_what_is_pending_and_then_releases_what_it_held(
    aops_pool: AsyncConnectionPool,
) -> None:
    agent_id, incarnation = await make_agent(aops_pool)
    main, sub = _running_foreground_sub_agent()
    reader = ScriptedReader({"main": main, "a1sub": sub})
    reader.visible.update({"main": 4, "a1sub": 0})
    pool = _FlakyPool(aops_pool)
    writer = make_writer(cast("AsyncConnectionPool", pool), agent_id, incarnation, reader, [])
    await _read_all(writer)
    reader.visible.update({"main": 5, "a1sub": 3})
    pool.fail_next = True
    with pytest.raises(OperationalError):
        _ = await writer.read_now()

    await writer.release_held()
    kinds = [k for k, _, _ in await timeline_of(aops_pool, agent_id)]
    assert kinds.count("subagent_text") == 3 and kinds.count("agent_code") == 2, (
        "the plan that was pending is committed and what the projector held is shown, in one close"
    )


async def test_the_synthetic_result_a_resume_writes_for_a_call_killed_mid_turn_is_paired_after_the_restart(
    aops_pool: AsyncConnectionPool,
) -> None:
    agent_id, incarnation = await make_agent(aops_pool)
    entries: list[AnyEntry] = [
        *_OPEN,
        ToolCall(key="c", order=Order(1), turn="t0", call_id="killed", name="Task", tool_input={}),
        HumanInput(key="i1", order=Order(2), turn="t1", wire_id="w1", text="continue"),
        ToolResult(
            key="synthetic",
            order=Order(3),
            turn="t1",
            call_id="killed",
            content="Tool call interrupted",
        ),
        AgentText(key="a1", order=Order(4), turn="t1", text="ok", message_ref="m1"),
        TurnEnd(key="e1", order=Order(5), turn="t1"),
    ]
    reader = ScriptedReader({"main": entries})
    reader.visible["main"] = 2
    first = make_writer(aops_pool, agent_id, incarnation, reader, [])
    await _read_all(first)

    second = make_writer(aops_pool, agent_id, incarnation, reader, [])
    reader.visible["main"] = len(entries)
    await _read_all(second)
    kinds = [k for k, _, _ in await timeline_of(aops_pool, agent_id)]
    assert kinds.count("code_output") == 1 and second.projector_sizes()["orphans"] == 0, (
        "after a kill and a resume under the same session id the CLI writes, at the start of the new turn, a result without any "
        "link for the call that was open: it is paired with that call"
    )

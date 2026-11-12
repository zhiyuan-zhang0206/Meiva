# meiva:note
# What the writer holds back, and for how long. A result whose call has not been read is on a clock of its own per call and is
# shown top-level when its own `HOLD_SECONDS` have passed, not when another's have. The output of a sub-agent whose link has
# not been read is on no clock: a foreground sub-agent's link comes with the result of the call that started it, however long
# it runs, so nothing that is only slow is released (and then nested half on top and half under the call); it is released
# when the turn it was born in closes, since nothing can link it any more. A call abandoned with its turn is still known
# after a restart when its result comes, within a bounded number of rows.
# meiva:link agent/harness_timeline/README.md

from __future__ import annotations

from psycopg_pool import AsyncConnectionPool

from agent.harness_timeline.tests.support import (
    ScriptedReader,
    make_agent,
    make_writer,
    timeline_of,
)
from agent.harness_timeline.writer import HOLD_SECONDS, Committed
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


async def test_what_is_held_for_a_link_is_released_top_level_when_its_turn_closes_and_reported(
    aops_pool: AsyncConnectionPool,
) -> None:
    agent_id, incarnation = await make_agent(aops_pool)
    main: list[AnyEntry] = [*_OPEN, *_task(1), *_CLOSE]
    reader = ScriptedReader({"main": main, "sub1": [_sub("sub1", 0)]})
    reader.visible.update({"main": 2, "sub1": 1})
    committed: list[Committed] = []
    writer = make_writer(aops_pool, agent_id, incarnation, reader, committed)
    while await writer.read_now():
        pass
    assert [k for k, _, _ in await timeline_of(aops_pool, agent_id)] == [
        "inbound_chat",
        "agent_code",
    ]

    reader.visible["main"] = 3
    while await writer.read_now():
        pass
    shown = await timeline_of(aops_pool, agent_id)
    assert [(kind, parent) for kind, _, parent in shown][-1] == ("subagent_text", None)
    anomalies = [a for c in committed for a in c.plan.anomalies]
    assert [(a.anomaly, a.detail) for a in anomalies] == [(AnomalyKind.UNLINKED_RELEASED, "sub1")]


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

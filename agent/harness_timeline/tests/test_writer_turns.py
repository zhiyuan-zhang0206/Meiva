# meiva:note
# A turn the stream reports ended is paired with the record's closing by identity, never by count: the writer knows which
# record turn each of our inputs was delivered in (the row that settled it) and which turns have closed, and the wait for
# a turn is over when the turns of its own inputs have closed. A closing that shows up late, a turn whose result frame
# never came (the process died after the closing was written) and the turns the CLI ran on its own between invocations
# cannot make a later turn's wait return early or compare another turn's spend.
# meiva:link agent/harness_timeline/README.md

from __future__ import annotations

from psycopg_pool import AsyncConnectionPool

from agent.harness_timeline.tests.support import (
    ScriptedReader,
    make_agent,
    make_inbound_row,
    make_writer,
    send_input,
)
from agent.harness_timeline.writer import HarnessTimelineWriter, TurnEndWait
from base.lm.harness.events import HarnessCause, Usage
from base.lm.harness.session.transcript.entries import (
    AgentText,
    AnyEntry,
    HumanInput,
    Notice,
    Order,
    TokenUsage,
    TurnEnd,
)

WAIT = {"quiet_seconds": 0.05, "ceiling_seconds": 2.0}


def _turn(index: int, *, closing: bool = True, spend: int = 0) -> list[AnyEntry]:
    base = 4 * index
    entries: list[AnyEntry] = [
        HumanInput(
            key=f"i{index}", order=Order(base), turn=f"t{index}", wire_id=f"w{index}", text="q"
        ),
        AgentText(
            key=f"a{index}",
            order=Order(base + 1),
            turn=f"t{index}",
            text="r",
            message_ref=f"m{index}",
        ),
        TokenUsage(
            key=f"u{index}",
            order=Order(base + 2),
            turn=f"t{index}",
            message_ref=f"m{index}",
            usage=Usage(input_tokens=spend, output_tokens=1),
        ),
    ]
    if closing:
        entries.append(TurnEnd(key=f"e{index}", order=Order(base + 3), turn=f"t{index}"))
    return entries


def _own_turn(index: int) -> list[AnyEntry]:
    base = 1_000 + 3 * index
    return [
        Notice(
            key=f"n{index}",
            order=Order(base),
            turn=f"own{index}",
            text="Background task done.",
            cause=HarnessCause.BACKGROUND_TASK,
        ),
        AgentText(
            key=f"oa{index}",
            order=Order(base + 1),
            turn=f"own{index}",
            text="ok",
            message_ref=f"om{index}",
        ),
        TurnEnd(key=f"oe{index}", order=Order(base + 2), turn=f"own{index}"),
    ]


async def _writer(
    pool: AsyncConnectionPool, entries: list[AnyEntry], inputs: int
) -> tuple[HarnessTimelineWriter, ScriptedReader]:
    agent_id, incarnation = await make_agent(pool)
    reader = ScriptedReader({"main": entries})
    writer = make_writer(pool, agent_id, incarnation, reader, [])
    for index in range(inputs):
        send_input(writer, f"w{index}", f"k{index}", await make_inbound_row(pool, agent_id))
    return writer, reader


async def _read_to(writer: HarnessTimelineWriter, reader: ScriptedReader, visible: int) -> None:
    reader.visible["main"] = visible
    while await writer.read_now():
        pass


async def test_a_closing_that_shows_up_late_does_not_end_the_waits_of_the_turns_after_it(
    aops_pool: AsyncConnectionPool,
) -> None:
    entries = [*_turn(0, closing=False), *_turn(1), *_turn(2), *_turn(3, closing=False)]
    writer, reader = await _writer(aops_pool, entries, 4)
    await _read_to(writer, reader, len(entries) - 1)

    assert await writer.wait_for_turn_end(["k0"], **WAIT) is TurnEndWait.QUIET, (
        "turn 0 is not closed"
    )
    assert await writer.wait_for_turn_end(["k1"], **WAIT) is TurnEndWait.CLOSED
    assert await writer.wait_for_turn_end(["k2"], **WAIT) is TurnEndWait.CLOSED
    assert await writer.wait_for_turn_end(["k3"], **WAIT) is TurnEndWait.QUIET, (
        "the late closing of turn 0, and the closings of the others, are not turn 3's"
    )
    entries.append(TurnEnd(key="e0", order=Order(2_000), turn="t0"))
    await _read_to(writer, reader, len(entries))
    assert await writer.wait_for_turn_end(["k0"], **WAIT) is TurnEndWait.CLOSED
    assert await writer.wait_for_turn_end(["k3"], **WAIT) is TurnEndWait.QUIET, "still not turn 3's"


async def test_a_turn_whose_result_frame_never_came_does_not_shift_the_next_turns_wait_or_spend(
    aops_pool: AsyncConnectionPool,
) -> None:
    entries = [*_turn(0, spend=100), *_turn(1, closing=False, spend=7)]
    writer, reader = await _writer(aops_pool, entries, 2)
    await _read_to(writer, reader, len(entries))
    assert writer.closed_usage(["k0"]) == Usage(input_tokens=100, output_tokens=1)

    assert await writer.wait_for_turn_end(["k1"], **WAIT) is TurnEndWait.QUIET, (
        "turn 0 closed and nobody waited for it: that closing is not turn 1's"
    )
    assert writer.closed_usage(["k1"]) is None, "turn 1 has not closed"
    entries.append(TurnEnd(key="e1", order=Order(500), turn="t1"))
    await _read_to(writer, reader, len(entries))
    assert await writer.wait_for_turn_end(["k1"], **WAIT) is TurnEndWait.CLOSED
    assert writer.closed_usage(["k1"]) == Usage(input_tokens=7, output_tokens=1), (
        "the spend of turn 1 and not of the turn closed before it"
    )


async def test_turns_the_cli_ran_on_its_own_are_not_waited_for_and_shift_nothing(
    aops_pool: AsyncConnectionPool,
) -> None:
    own = [entry for index in range(5) for entry in _own_turn(index)]
    entries = [*own, *_turn(0, closing=False, spend=3)]
    writer, reader = await _writer(aops_pool, entries, 1)
    await _read_to(writer, reader, len(entries))

    assert await writer.wait_for_turn_end([], **WAIT) is TurnEndWait.CLOSED, (
        "no input of ours in it"
    )
    assert await writer.wait_for_turn_end(["k0"], **WAIT) is TurnEndWait.QUIET, (
        "five closings of the CLI's own turns are not the closing of ours"
    )
    entries.append(TurnEnd(key="e0", order=Order(3_000), turn="t0"))
    await _read_to(writer, reader, len(entries))
    assert await writer.wait_for_turn_end(["k0"], **WAIT) is TurnEndWait.CLOSED
    assert writer.closed_usage(["k0"]) == Usage(input_tokens=3, output_tokens=1)
    assert writer.closed_usage([]) is None


async def test_a_steer_and_the_input_that_opened_the_turn_are_waited_for_as_one_turn(
    aops_pool: AsyncConnectionPool,
) -> None:
    entries: list[AnyEntry] = [
        *_turn(0, closing=False, spend=5)[:3],
        HumanInput(
            key="s0", order=Order(10), turn="t0", wire_id="w1", text="q", joined_running_turn=True
        ),
    ]
    writer, reader = await _writer(aops_pool, entries, 2)
    await _read_to(writer, reader, len(entries))
    assert await writer.wait_for_turn_end(["k0", "k1"], **WAIT) is TurnEndWait.QUIET
    entries.append(TurnEnd(key="e0", order=Order(20), turn="t0"))
    await _read_to(writer, reader, len(entries))
    assert await writer.wait_for_turn_end(["k0", "k1"], **WAIT) is TurnEndWait.CLOSED
    assert writer.closed_usage(["k0", "k1"]) == Usage(input_tokens=5, output_tokens=1), (
        "one turn's spend, counted once"
    )

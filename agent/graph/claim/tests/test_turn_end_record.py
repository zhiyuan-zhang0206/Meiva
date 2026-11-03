# meiva:note
# `settle_turn_end` is the turn boundary: the held model call becomes one `turn_end` record,
# and the reason recorded on it is the one this decision point resolved — never a guess made
# where the event is written.
# meiva:link agent/graph/llm/docs/turn-records.meiva.okf.md
from __future__ import annotations

from langchain_core.messages import HumanMessage
from langgraph.types import Command

from agent.graph.claim.turn_end import settle_turn_end
from agent.nodes import BEFORE_LLM, END
from agent.state import AgentState
from base.log.tests.log_records import LogRecords


def _state(
    *,
    turn_end_held_seconds: float | None = None,
    ended_with: str | None = None,
    ended_with_reason: str | None = None,
) -> AgentState:
    return AgentState(
        messages=[HumanMessage(content="hi")],
        turn_end_held_seconds=turn_end_held_seconds,
        ended_with=ended_with,
        ended_with_reason=ended_with_reason,
    )


def _records(loguru_records: LogRecords) -> list[dict[str, object]]:
    return [record["extra"] for record in loguru_records.events("turn_end")]


def test_held_call_becomes_no_input_at_the_turn_end(loguru_records: LogRecords) -> None:
    command = settle_turn_end(_state(turn_end_held_seconds=2.5), Command(goto=END), agent_id=7)

    (record,) = _records(loguru_records)
    assert record["ended_with"] == "no_input"
    assert record["duration_seconds"] == 2.5
    assert record["ok"] is True
    assert record["agent_id"] == 7
    assert command.update is not None
    assert command.update["turn_end_held_seconds"] is None, (
        "the held call must not reach the next turn"
    )


# meiva:note
# `invalid` is a fact of its own: a declaration was made and refused. It must reach the
# record as `invalid` rather than collapsing into the `no_input` default.
def test_a_refused_declaration_reaches_the_record_as_invalid(loguru_records: LogRecords) -> None:
    state = _state(turn_end_held_seconds=4.0, ended_with="invalid")

    settle_turn_end(state, Command(goto=END), agent_id=7)

    (record,) = _records(loguru_records)
    assert record["ended_with"] == "invalid"


def test_a_declared_reason_wins_over_the_boundary_default(loguru_records: LogRecords) -> None:
    state = _state(
        turn_end_held_seconds=9.0,
        ended_with="deferred:time",
        ended_with_reason="night, continue after 09:00",
    )

    settle_turn_end(state, Command(goto=END), agent_id=7)

    (record,) = _records(loguru_records)
    assert record["ended_with"] == "deferred:time"
    assert record["ended_with_reason"] == "night, continue after 09:00"


# meiva:note
# The halt was not this turn's end: new input arrived, so the loop ran another round.
def test_a_continuing_turn_emits_the_record_without_a_reason(loguru_records: LogRecords) -> None:
    state = _state(turn_end_held_seconds=1.5, ended_with="done")

    command = settle_turn_end(state, Command(goto=BEFORE_LLM), agent_id=7)

    (record,) = _records(loguru_records)
    assert "ended_with" not in record, "a turn that continues has not ended, so it has no reason"
    assert command.update is not None
    assert command.update["ended_with"] is None, "a stale declaration must not reach the next round"


# meiva:note
# Terminate / restart ended the turn; the loop did not run out of input, so it stays silent.
def test_a_lifecycle_end_is_not_recorded_as_running_out_of_input(
    loguru_records: LogRecords,
) -> None:
    state = _state(turn_end_held_seconds=3.0)

    settle_turn_end(state, Command(goto=END, update={"exit_requested": True}), agent_id=7)

    (record,) = _records(loguru_records)
    assert "ended_with" not in record


# meiva:note
# exec → claim → llm: the claim between a declaration and the halt must not eat it.
def test_a_declaration_survives_the_claim_pass_before_the_halt(loguru_records: LogRecords) -> None:
    command: Command[str] = Command(goto=BEFORE_LLM)

    assert settle_turn_end(_state(ended_with="done"), command, agent_id=7) is command


def test_nothing_held_and_nothing_declared_costs_one_read(loguru_records: LogRecords) -> None:
    command: Command[str] = Command(goto=BEFORE_LLM)

    assert settle_turn_end(_state(), command, agent_id=7) is command
    assert _records(loguru_records) == []

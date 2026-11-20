# meiva:note
# `settle_turn_end` is the turn boundary: the held model call becomes one `turn_end` record,
# and the reason recorded on it is the one this decision point resolved — never a guess made
# where the event is written.
# meiva:link agent/graph/llm/docs/turn-records.md
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from langchain_core.messages import HumanMessage
from langgraph.types import Command

from agent.graph.claim.harness_read_sink import ReadTimelineSink
from agent.graph.claim.tests.claim_support import update_of
from agent.graph.claim.turn_end import settle_turn_end
from agent.nodes import BEFORE_TURN, END
from agent.state import AgentState
from base.agents.context import MeivaContext
from base.api_contracts.agent_contract import Harness
from base.clock import SYSTEM_TIMERS, Clock, ClockConfig
from base.config.tests.configuration import defaults
from base.events.live.projection import TimelineSnapshot
from base.events.live.projection import TurnStarted as ProjectionTurnStarted
from base.events.live.tests.live_capture import LiveEvents
from base.events.live.tests.live_capture import live_events as live_events
from base.host.env.agent_slices import AgentSlices
from base.lm.harness import Usage
from base.lm.harness.engine import HarnessTurn
from base.lm.harness.registry import TurnTimingStore
from base.log.tests.log_records import LogRecords
from base.paths import harness_sessions_dir


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
    assert update_of(command)["turn_end_held_seconds"] is None, (
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

    command = settle_turn_end(state, Command(goto=BEFORE_TURN), agent_id=7)

    (record,) = _records(loguru_records)
    assert "ended_with" not in record, "a turn that continues has not ended, so it has no reason"
    assert update_of(command)["ended_with"] is None, (
        "a stale declaration must not reach the next round"
    )


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
    command: Command[str] = Command(goto=BEFORE_TURN)

    assert settle_turn_end(_state(ended_with="done"), command, agent_id=7) is command


def test_nothing_held_and_nothing_declared_costs_one_read(loguru_records: LogRecords) -> None:
    command: Command[str] = Command(goto=BEFORE_TURN)

    assert settle_turn_end(_state(), command, agent_id=7) is command
    assert _records(loguru_records) == []


def _write_agy_step(record_path: Path) -> None:
    record_path.parent.mkdir(parents=True, exist_ok=True)
    record_path.write_text(
        json.dumps(
            {
                "type": "PLANNER_RESPONSE",
                "status": "DONE",
                "step_index": 0,
                "created_at": "2026-11-19T07:00:02Z",
                "thinking": "checking",
                "content": "",
                "tool_calls": [{"name": "run_command", "args": {"CommandLine": "echo ok"}}],
            }
        )
        + "\n"
    )


def _setup_plan_b_sink(
    tmp_path: Path,
    live_events: LiveEvents,
    session: str,
    current: list[datetime],
) -> tuple[ReadTimelineSink, TurnTimingStore, Path]:
    home = tmp_path / "meiva_home"
    record_path = (
        tmp_path
        / ".gemini/antigravity-cli/brain"
        / session
        / ".system_generated/logs/transcript_full.jsonl"
    )
    clock = Clock(
        ClockConfig(timezone="UTC", authoritative_timezone=None, message_timestamp_weekday=False),
        timers=SYSTEM_TIMERS,
        now=lambda: current[0],
    )
    ctx = MeivaContext(
        agent=AgentSlices.resolve(default_reader=defaults()),
        harness=Harness.ANTIGRAVITY,
        meiva_home=home,
        environ={"HOME": str(tmp_path)},
        clock_factory=lambda: clock,
    )
    sink = ReadTimelineSink(
        ctx, 100, live_events.publisher, non_human_wake=False, session_id=lambda: session
    )
    sink._floor = 0
    return sink, TurnTimingStore(harness_sessions_dir(home=home)), record_path


async def _last_snapshot(live_events: LiveEvents) -> TimelineSnapshot:
    snaps = await live_events.of_type(TimelineSnapshot)
    assert snaps
    return snaps[-1]


async def test_read_sink_plan_b_turn_timing_live_to_settled(
    tmp_path: Path,
    live_events: LiveEvents,
) -> None:
    session = "agy-turn-timing"
    t0 = datetime(2026, 11, 19, 7, 0, 0, tzinfo=UTC)
    current = [t0]
    sink, store, record_path = _setup_plan_b_sink(tmp_path, live_events, session, current)

    sink.on_turn_started("t-agy-1")
    started = await live_events.typed()
    assert len(started) == 1 and isinstance(started[0], ProjectionTurnStarted)
    assert started[0].turn_id == "t-agy-1" and started[0].started_at == t0.isoformat()

    spans_mid, active_mid = store.load(100, "antigravity", session_id=session)
    assert spans_mid == () and active_mid is not None and active_mid.turn_id == "t-agy-1"

    _write_agy_step(record_path)
    current[0] += timedelta(seconds=3)
    await sink._read()
    mid_snap = await _last_snapshot(live_events)
    assert (
        mid_snap.active_turn_id == "t-agy-1" and mid_snap.active_turn_started_at == t0.isoformat()
    )
    assert all(
        it["turn_id"] == "t-agy-1" and it["turn_duration_ms"] is None for it in mid_snap.items
    )

    current[0] += timedelta(seconds=3, milliseconds=500)
    await sink.finish(
        (
            HarnessTurn(
                turn_id="t-agy-1",
                text="",
                usage=Usage(),
                cost_usd=None,
                subtype="success",
                is_error=False,
                interrupted=False,
            ),
        ),
        turn_id="t-agy-1",
        ended_at=current[0],
        fallback_duration_ms=0,
    )
    closing = await _last_snapshot(live_events)
    assert closing.active_turn_id is None and closing.active_turn_started_at is None
    work = [
        it for it in closing.items if it["kind"] in ("agent_reasoning", "agent_code", "code_output")
    ]
    assert work and all(
        it["turn_id"] == "t-agy-1" and it["turn_duration_ms"] == 6500 for it in work
    )

    spans_after, active_after = store.load(100, "antigravity", session_id=session)
    assert active_after is None and len(spans_after) == 1
    assert spans_after[0].turn_id == "t-agy-1" and spans_after[0].duration_ms == 6500

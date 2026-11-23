# meiva:note
# `settle_turn_end` is the turn boundary: the held model call becomes one `turn_end` record,
# and the reason recorded on it is the one this decision point resolved — never a guess made
# where the event is written.
# meiva:link agent/graph/llm/docs/turn-records.md
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, replace
from pathlib import Path

import psycopg
import pytest
from langchain_core.messages import HumanMessage
from langgraph.runtime import Runtime
from langgraph.types import Command

from agent.graph.claim.harness_activity import log_turn_usage
from agent.graph.claim.harness_read_sink import ReadTimelineSink
from agent.graph.claim.harness_turn import harness_turn_node
from agent.graph.claim.tests.claim_support import snapshot_items
from agent.graph.claim.tests.harness_support import (
    FakeEngine,
    RuntimeFixture,
    completed,
    config,
    in_a_turn,
    runtime,
    silent_engine,
)
from agent.graph.claim.tests.harness_support import harness_runtime as harness_runtime
from agent.graph.claim.tests.harness_support import hosted_resources as hosted_resources
from agent.graph.claim.turn_end import settle_turn_end
from agent.nodes import BEFORE_TURN, END
from agent.ownership.tests.ownership_support import agent_row
from agent.state import AgentState
from base.agents.context import MeivaContext
from base.agents.tests.langgraph_views import command_update
from base.api_contracts.agent_contract import Harness
from base.api_contracts.usage import UsageLedgerWriteError
from base.clock.tests.fakes import system_clock
from base.config.tests.configuration import defaults
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.db.usage_ledger import bind_usage_ledger, usage_writer
from base.events.live.bus import EventBus
from base.events.live.tests.live_capture import LiveEvents
from base.host.env.agent_slices import AgentSlices
from base.lm.harness import (
    AssistantText,
    BridgeEvent,
    FileSessionRegistry,
    HarnessInput,
    HarnessTurn,
    TurnCompleted,
    TurnOrigin,
    TurnStarted,
    Usage,
    completed_turn,
)
from base.lm.harness.antigravity.tests.support import seed_replay_history, start
from base.lm.harness.base_session import BaseHarnessSession, SessionBinding
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
    assert command_update(command)["turn_end_held_seconds"] is None, (
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
    assert command_update(command)["ended_with"] is None, (
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


def test_incomplete_harness_usage_is_marked_on_the_usage_report(loguru_records: LogRecords) -> None:
    ctx = runtime(silent_engine()).context
    turn = HarnessTurn(
        turn_id="t1",
        text="done",
        usage=Usage(17_349, 106, 0, 15_032),
        cost_usd=None,
        subtype="success",
        is_error=False,
        interrupted=False,
        usage_uncertain=True,
    )
    log_turn_usage(ctx, 7684, "gemini-4-hydrogen", turn)
    [row] = loguru_records.events("harness_usage_uncertain")
    assert row["extra"]["tok_in"] == 17_349 and row["extra"]["tok_out"] == 106
    [report] = loguru_records.events("llm_usage")
    assert report["extra"]["complete"] is False
    assert report["extra"]["in_total"] == 17_349


def _write_native_planner(path: Path, **fields: object) -> None:
    with path.open("a", encoding="utf-8") as handle:
        _ = handle.write(
            json.dumps({"type": "PLANNER_RESPONSE", "status": "DONE", **fields}) + "\n"
        )


@pytest.mark.parametrize(
    ("prefix", "delivered"),
    [("MEIVA_HIDE_FROM_USER", False), ("MEIVA_DISPLAY_TO_USER", True)],
)
async def test_completed_native_task_uses_recorded_reply_visibility(
    prefix: str,
    delivered: bool,
    db_conn: psycopg.Connection,
    database_gate: ProcessDbGate,
    event_bus: EventBus,
    tmp_path: Path,
    live_events: LiveEvents,
) -> None:
    aid, sid = agent_row(db_conn), "completed-native-task"
    path = (
        tmp_path
        / ".gemini/antigravity-cli/brain"
        / sid
        / ".system_generated/logs/transcript_full.jsonl"
    )
    path.parent.mkdir(parents=True)
    _write_native_planner(
        path, created_at="2026-11-22T19:41:44Z", content="MEIVA_DISPLAY_TO_USER\nOld reply"
    )
    start = path.stat().st_size
    with path.open("a") as handle:
        _ = handle.write(
            json.dumps(
                {
                    "type": "SYSTEM_MESSAGE",
                    "status": "DONE",
                    "created_at": "2026-11-22T19:42:13Z",
                    "content": f"[Message] timestamp=2026-11-22T19:42:13Z sender={sid}/task-2820 priority=MESSAGE_PRIORITY_HIGH content=timer prompt",
                }
            )
            + "\n"
        )
    offset = path.stat().st_size
    reply = f"{prefix}\nTIMER_PURPLE"
    _write_native_planner(path, created_at="2026-11-22T19:43:05Z", content=reply)
    ctx = MeivaContext(
        agent=AgentSlices.resolve({"user_delivery_mode": "prefix"}, default_reader=defaults()),
        harness=Harness.ANTIGRAVITY,
        meiva_home=tmp_path,
        db=Database.from_settings(gate=database_gate),
        bus=event_bus,
        clock_factory=system_clock,
        environ={"HOME": str(tmp_path)},
    )
    for index in range(2):
        sink = ReadTimelineSink(
            ctx,
            aid,
            live_events.publisher,
            non_human_wake=True,
            session_id=lambda: sid,
        )
        if index == 0:
            await sink.finish()
            assert (
                db_conn.execute(
                    "SELECT content FROM agent_publications WHERE agent_id = %s", (aid,)
                ).fetchall()
                == []
            )
        await sink.on_event(
            TurnStarted(
                "native", TurnOrigin.HARNESS, (), 1, record_start=start, record_reply=offset
            )
        )
        await sink.on_event(AssistantText("native", reply))
        await sink.finish()
    db_conn.commit()
    assert db_conn.execute(
        "SELECT content FROM agent_publications WHERE agent_id = %s", (aid,)
    ).fetchall() == ([("TIMER_PURPLE",)] if delivered else [])
    assert (
        db_conn.execute("SELECT id FROM inbound_messages WHERE agent_id = %s", (aid,)).fetchall()
        == []
    )
    snapshots = [
        e for e in await live_events.object_events() if e.get("role") == "timeline_snapshot"
    ]
    assert any(
        item.get("kind") == "agent_chat"
        and item.get("user_facing") is delivered
        and "TIMER_PURPLE" in str(item.get("payload"))
        for item in snapshot_items(snapshots)
    )


async def test_resumed_acp_replay_cannot_publish_historical_output(
    db_conn: psycopg.Connection,
    database_gate: ProcessDbGate,
    event_bus: EventBus,
    tmp_path: Path,
    live_events: LiveEvents,
) -> None:
    aid = agent_row(db_conn)
    seed_replay_history(tmp_path)
    harness = await start(tmp_path, resume="conv-acp", extra_args=("--test-late-load-replay",))
    ctx = MeivaContext(
        agent=AgentSlices.resolve({"user_delivery_mode": "prefix"}, default_reader=defaults()),
        harness=Harness.ANTIGRAVITY,
        meiva_home=tmp_path,
        db=Database.from_settings(gate=database_gate),
        bus=event_bus,
        clock_factory=system_clock,
        environ={"HOME": str(harness.home)},
    )
    sink = ReadTimelineSink(
        ctx,
        aid,
        live_events.publisher,
        non_human_wake=False,
        session_id=lambda: harness.session.session_id,
    )
    try:
        await harness.session.deliver([HarnessInput.of("fresh", "resumed")])
        done = await harness.wait_for_type(TurnCompleted)
        for event in harness.events:
            await sink.on_event(event)
        await sink.finish()
        db_conn.commit()
        assert done.usage == Usage(250, 3, 0, 30) and not done.usage_uncertain
        assert db_conn.execute(
            "SELECT content FROM agent_publications WHERE agent_id = %s", (aid,)
        ).fetchall() == [("Fresh reply",)]
        assert (
            db_conn.execute(
                "SELECT id FROM inbound_messages WHERE agent_id = %s", (aid,)
            ).fetchall()
            == []
        )
    finally:
        await harness.stop()


@dataclass(frozen=True)
class _UsageSessionConfig:
    cwd: Path
    model: str = "deepseek-v4-pro"
    cancel_settle_seconds: float = 1.0
    max_buffered_events: int = 16


@pytest.mark.parametrize("harness", (Harness.CLAUDE_CODE, Harness.CODEX, Harness.ANTIGRAVITY))
def test_restored_harness_generation_books_new_reports_once(
    harness: Harness, database: Database, tmp_path: Path
) -> None:
    source = harness.value

    class Session(BaseHarnessSession[_UsageSessionConfig]):
        harness = source

        def first_report(self, session_id: str) -> TurnCompleted:
            self._session_id = session_id
            generation = self._begin_generation()
            self._bind()
            event = completed(self._mint_turn_id(), "done", usage=Usage(100, 10))
            return replace(event, session_id=session_id, generation=generation)

    ctx = runtime(silent_engine()).context
    ledger = bind_usage_ledger(lambda: database, lambda: "test", tmp_path)
    ctx = replace(
        ctx, harness=harness, catalog=replace(ctx.require_catalog(), usage_ledger=lambda: ledger)
    )
    for _ in range(2):
        registry = FileSessionRegistry(tmp_path / "sessions")
        session = Session(
            _UsageSessionConfig(tmp_path), binding=SessionBinding(7, registry), clock=system_clock()
        )
        provider_session = session._resume_from_registry() or "resumed-session"
        event = session.first_report(provider_session)
        assert event.turn_id == "turn-1"
        log_turn_usage(ctx, 7, session.model, completed_turn(event, ""))
        log_turn_usage(ctx, 7, session.model, completed_turn(event, ""))
    with database.connect() as conn:
        rows = conn.execute("SELECT source, tokens_in FROM llm_usage_ledger").fetchall()
    assert rows == [(source, 100), (source, 100)]


async def test_locked_ledger_preserves_every_report_and_finishes_the_turn(
    database: Database,
    db_url: str,
    db_conn: psycopg.Connection,
    tmp_path: Path,
    loguru_records: LogRecords,
    live_events: LiveEvents,
    harness_runtime: RuntimeFixture,
) -> None:
    attempts = 0
    writing = asyncio.Event()
    loop = asyncio.get_running_loop()

    def connect() -> psycopg.Connection:
        nonlocal attempts
        attempts += 1
        _ = loop.call_soon_threadsafe(writing.set)
        return psycopg.connect(db_url, options="-c statement_timeout=80")

    reply = "MEIVA_DISPLAY_TO_USER\npaid reply"

    def turn(inputs: tuple[HarnessInput, ...]) -> list[BridgeEvent]:
        events = in_a_turn([item.key for item in inputs], reply)
        terminal = events[-1]
        assert isinstance(terminal, TurnCompleted)
        events[-1] = replace(terminal, steps=(Usage(100, 10), Usage(200, 20)))
        return events

    agent_id = agent_row(db_conn)
    engine = FakeEngine(script=lambda inputs: turn(tuple(inputs)))
    ctx = harness_runtime(engine, agent_id=agent_id, publisher=live_events.publisher).context
    ledger = bind_usage_ledger(lambda: database, lambda: "test", tmp_path)
    ledger = replace(ledger, write=usage_writer(connect))
    ctx = replace(ctx, catalog=replace(ctx.require_catalog(), usage_ledger=lambda: ledger))
    state = AgentState(harness_inputs=[{"key": "test-input", "prompt": "do it"}])
    with psycopg.connect(db_url) as blocker:
        blocker.execute("LOCK TABLE llm_usage_ledger IN ACCESS EXCLUSIVE MODE")
        task = asyncio.create_task(harness_turn_node(state, Runtime(context=ctx), config(agent_id)))
        await asyncio.wait_for(writing.wait(), timeout=10)
        assert not task.done()
        with pytest.raises(UsageLedgerWriteError) as failure:
            await task
        assert isinstance(failure.value.__cause__, psycopg.errors.QueryCanceled)
    assert attempts == 4
    assert [event["role"] for event in await live_events.object_events()].count("llm_done") == 1
    observations = loguru_records.events("llm_usage")
    assert [record["extra"]["in_total"] for record in observations] == [100, 200]
    failed = loguru_records.events("llm_usage_ledger_write_failed")
    assert [record["extra"]["call_id"] for record in observations] == [
        record["extra"]["call_id"] for record in failed
    ]
    with psycopg.connect(db_url) as conn:
        assert conn.execute("SELECT count(*) FROM llm_usage_ledger").fetchone() == (0,)

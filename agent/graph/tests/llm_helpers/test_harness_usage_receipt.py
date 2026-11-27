from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from concurrent.futures import Future
from dataclasses import replace
from pathlib import Path
from threading import Event

import psycopg
import pytest
from langgraph.runtime import Runtime

from agent.graph.claim.harness_activity import stage_turn_usage
from agent.graph.claim.harness_turn import harness_turn_node
from agent.graph.claim.tests.harness_support import (
    FakeEngine,
    FakeSubscription,
    RuntimeFixture,
    completed,
    config,
    in_a_turn,
    runtime,
    silent_engine,
    wait_until,
)
from agent.graph.claim.tests.harness_support import harness_runtime as harness_runtime
from agent.graph.claim.tests.harness_support import hosted_resources as hosted_resources
from agent.graph.llm_errors import FatalLLMStreamError
from agent.ownership.tests.ownership_support import agent_row
from agent.state import AgentState
from base.agents.context import MeivaContext
from base.api_contracts.usage import (
    ReceiptFallbackReporter,
    ReceiptSubmission,
    ScheduledUsageReceipt,
    UsageLedgerEntry,
    UsageReceiptSubmitter,
)
from base.db import Database
from base.db.usage_ledger import bind_usage_ledger, usage_writer
from base.events.live.tests.live_capture import LiveEvents
from base.events.live.tests.live_capture import live_events as live_events
from base.lm.harness import (
    BridgeEvent,
    HarnessInput,
    HarnessTurnFaultedError,
    TurnCompleted,
    Usage,
    completed_turn,
)
from base.lm.usage.receipt import (
    UsageReceiptDelivery,
    UsageReceiptRuntime,
    UsageReceiptStore,
    decode_receipt,
)
from base.log.tests.log_records import LogRecords
from base.log.tests.log_records import loguru_records as loguru_records


def _record_admission(owner: UsageReceiptRuntime, accepted: list[str]) -> ScheduledUsageReceipt:
    def schedule(
        entry: UsageLedgerEntry, submit: UsageReceiptSubmitter, observe: Callable[[], None]
    ) -> Future[None]:
        accepted.append(entry.call_id)
        return owner.schedule_completed(entry, submit, observe)

    return ScheduledUsageReceipt(owner.submit, schedule)


async def _assert_provider_outcome(
    state: AgentState, ctx: MeivaContext, agent_id: int, *, faulted: bool
) -> None:
    if not faulted:
        _ = await harness_turn_node(state, Runtime(context=ctx), config(agent_id))
        return
    with pytest.raises(FatalLLMStreamError, match="provider reported failure") as error:
        _ = await harness_turn_node(state, Runtime(context=ctx), config(agent_id))
    assert isinstance(error.value.__cause__, HarnessTurnFaultedError)
    assert "provider reported failure" in str(error.value.__cause__)


@pytest.mark.parametrize("faulted", [False, True])
async def test_completed_harness_turn_replays_frozen_receipts_after_postgres_timeout(
    database: Database,
    db_url: str,
    db_conn: psycopg.Connection,
    tmp_path: Path,
    loguru_records: LogRecords,
    live_events: LiveEvents,
    harness_runtime: RuntimeFixture,
    faulted: bool,
) -> None:
    def connect() -> psycopg.Connection:
        return psycopg.connect(db_url, options="-c statement_timeout=80")

    def turn(inputs: Sequence[HarnessInput]) -> list[BridgeEvent]:
        events = in_a_turn([item.key for item in inputs], "MEIVA_DISPLAY_TO_USER\npaid reply")
        terminal = events[-1]
        assert isinstance(terminal, TurnCompleted)
        events[-1] = replace(
            terminal,
            is_error=faulted,
            errors=("provider reported failure",) if faulted else (),
            steps=(Usage(100, 10), Usage(200, 20)),
        )
        return events

    agent_id = agent_row(db_conn)
    engine = FakeEngine(script=turn)
    ctx = harness_runtime(engine, agent_id=agent_id, publisher=live_events.publisher).context
    healthy = bind_usage_ledger(lambda: database, lambda: "test", tmp_path)
    ledger = replace(healthy, write=usage_writer(connect))
    store = UsageReceiptStore(tmp_path, ledger.cluster)
    owner = UsageReceiptRuntime(store, ledger)
    accepted: list[str] = []
    ctx = replace(
        ctx,
        catalog=replace(ctx.require_catalog(), usage_ledger=lambda: ledger),
        usage_receipts=_record_admission(owner, accepted),
    )
    state = AgentState(harness_inputs=[{"key": "test-input", "prompt": "do it"}])
    owner.start()
    with psycopg.connect(db_url) as blocker:
        _ = blocker.execute("LOCK TABLE llm_usage_ledger IN ACCESS EXCLUSIVE MODE")
        try:
            async with asyncio.timeout(10):
                await _assert_provider_outcome(state, ctx, agent_id, faulted=faulted)
            await wait_until(
                lambda: bool(loguru_records.events("llm_usage_ledger_write_failed")),
                "real Postgres ledger timeout",
            )
        finally:
            await owner.aclose(staging_timeout=1, delivery_timeout=1)

    assert len(engine.delivered) == 1
    assert [event["role"] for event in await live_events.object_events()].count("llm_done") == int(
        not faulted
    )
    pending = [payload for _, payload in store.pending(after=0, limit=3)]
    entries = [decode_receipt(payload) for payload in pending]
    assert [entry.tokens_in for entry in entries] == [100, 200]
    assert len({entry.call_id for entry in entries}) == 2
    assert [entry.call_id for entry in entries] == accepted
    assert all(entry.price_snapshot and entry.cost_usd for entry in entries)
    with database.connect() as conn:
        assert conn.execute("SELECT count(*) FROM llm_usage_ledger").fetchone() == (0,)

    delivery = UsageReceiptDelivery(store, healthy, poll_seconds=0.01)
    delivery.start()
    try:
        await wait_until(lambda: store.backlog().pending == 0, "first ledger delivery")
        for payload in pending:
            store.recover(payload)
        await wait_until(lambda: store.backlog().pending == 0, "matching replay")
    finally:
        await delivery.aclose(timeout=1)
    with database.connect() as conn:
        rows = conn.execute(
            "SELECT call_id, cost_usd, price_snapshot FROM llm_usage_ledger ORDER BY call_id"
        ).fetchall()
    assert rows == [(entry.call_id, entry.cost_usd, entry.price_snapshot) for entry in entries]


async def test_turn_cancellation_preserves_all_accepted_steps(
    tmp_path: Path, database: Database
) -> None:
    ledger = bind_usage_ledger(lambda: database, lambda: "test", tmp_path)
    store = UsageReceiptStore(tmp_path, ledger.cluster)
    owner = UsageReceiptRuntime(store, ledger)
    entered = asyncio.Event()
    release = Event()
    loop = asyncio.get_running_loop()

    def submit(
        entry: UsageLedgerEntry, *, report_fallback: ReceiptFallbackReporter | None = None
    ) -> ReceiptSubmission:
        _ = loop.call_soon_threadsafe(entered.set)
        assert release.wait(5)
        return owner.submit(entry, report_fallback=report_fallback)

    ctx = runtime(silent_engine()).context
    ctx = replace(
        ctx,
        catalog=replace(ctx.require_catalog(), usage_ledger=lambda: ledger),
        usage_receipts=ScheduledUsageReceipt(submit, owner.schedule_completed),
    )
    event = replace(completed("turn-1", "reply"), steps=(Usage(100, 10), Usage(200, 20)))
    turn = completed_turn(event, "")
    owner.start()
    task = asyncio.create_task(stage_turn_usage(ctx, 7, "claude-sonnet-5-5", turn))
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        _ = task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
        await owner.aclose(staging_timeout=2, delivery_timeout=1)

    pending = [decode_receipt(payload).tokens_in for _, payload in store.pending(after=0, limit=3)]
    with database.connect() as conn:
        posted = [row[0] for row in conn.execute("SELECT tokens_in FROM llm_usage_ledger")]
    assert set(pending + posted) == {100, 200}


class _CancellingSubscription(FakeSubscription):
    def __init__(self, engine: FakeEngine, on_terminal: Callable[[TurnCompleted], None]) -> None:
        super().__init__(engine)
        self._on_terminal = on_terminal

    async def get(self) -> BridgeEvent | None:
        event = await super().get()
        if isinstance(event, TurnCompleted):
            self._on_terminal(event)
        return event


class _CancellingEngine(FakeEngine):
    def __init__(
        self, terminal: TurnCompleted, on_terminal: Callable[[TurnCompleted], None]
    ) -> None:
        def script(inputs: Sequence[HarnessInput]) -> list[BridgeEvent]:
            events = in_a_turn([item.key for item in inputs], terminal.text)
            events[-1] = terminal
            return events

        super().__init__(script=script)
        self._on_terminal = on_terminal

    async def subscribe(self) -> FakeSubscription:
        subscription = _CancellingSubscription(self, self._on_terminal)
        self.subscriptions.add(subscription)
        return subscription


@pytest.mark.parametrize("faulted", [False, True])
async def test_terminal_usage_survives_cancellation_in_the_reader_iteration(
    database: Database,
    db_conn: psycopg.Connection,
    tmp_path: Path,
    harness_runtime: RuntimeFixture,
    faulted: bool,
) -> None:
    aid = agent_row(db_conn)
    frame = completed("t1", "MEIVA_DISPLAY_TO_USER\npaid reply", usage=Usage(333, 18))
    frame = replace(
        frame, is_error=faulted, errors=("provider reported failure",) if faulted else ()
    )
    loop = asyncio.get_running_loop()
    reader_frames: list[TurnCompleted] = []

    def on_terminal(event: TurnCompleted) -> None:
        reader_frames.append(event)
        _ = loop.call_soon(turn_task.cancel)

    engine = _CancellingEngine(frame, on_terminal)
    ctx = harness_runtime(engine, agent_id=aid).context
    ledger = bind_usage_ledger(lambda: database, lambda: "test", tmp_path)
    store = UsageReceiptStore(tmp_path, ledger.cluster)
    committed = asyncio.Event()

    def write(entry: UsageLedgerEntry) -> bool:
        result = ledger.write(entry)
        _ = loop.call_soon_threadsafe(committed.set)
        return result

    owner = UsageReceiptRuntime(store, replace(ledger, write=write))
    accepted: list[str] = []

    ctx = replace(
        ctx,
        catalog=replace(ctx.require_catalog(), usage_ledger=lambda: ledger),
        usage_receipts=_record_admission(owner, accepted),
    )
    state = AgentState(harness_inputs=[{"key": "test-input", "prompt": "do it"}])
    owner.start()
    turn_task = asyncio.create_task(harness_turn_node(state, Runtime(context=ctx), config(aid)))
    try:
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(turn_task, timeout=10)
        assert reader_frames == [frame]
        assert len(engine.delivered) == 1
        [call_id] = accepted
        await asyncio.wait_for(committed.wait(), timeout=10)
    finally:
        await owner.aclose(staging_timeout=2, delivery_timeout=1)

    with database.connect() as conn:
        assert conn.execute(
            "SELECT call_id, tokens_in, tokens_out FROM llm_usage_ledger"
        ).fetchall() == [(call_id, 333, 18)]

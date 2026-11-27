from __future__ import annotations

import asyncio
from collections.abc import Sequence
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
from base.api_contracts.usage import (
    ReceiptFallbackReporter,
    ReceiptSubmission,
    ScheduledUsageReceipt,
    UsageLedgerEntry,
)
from base.db import Database
from base.db.usage_ledger import bind_usage_ledger, usage_writer
from base.events.live.tests.live_capture import LiveEvents
from base.events.live.tests.live_capture import live_events as live_events
from base.lm.harness import BridgeEvent, HarnessInput, TurnCompleted, Usage, completed_turn
from base.lm.usage.receipt import (
    UsageReceiptDelivery,
    UsageReceiptRuntime,
    UsageReceiptStore,
    decode_receipt,
)
from base.log.tests.log_records import LogRecords
from base.log.tests.log_records import loguru_records as loguru_records


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
        events[-1] = replace(terminal, is_error=faulted, steps=(Usage(100, 10), Usage(200, 20)))
        return events

    agent_id = agent_row(db_conn)
    engine = FakeEngine(script=turn)
    ctx = harness_runtime(engine, agent_id=agent_id, publisher=live_events.publisher).context
    healthy = bind_usage_ledger(lambda: database, lambda: "test", tmp_path)
    ledger = replace(healthy, write=usage_writer(connect))
    store = UsageReceiptStore(tmp_path, ledger.cluster)
    owner = UsageReceiptRuntime(store, ledger)
    ctx = replace(
        ctx,
        catalog=replace(ctx.require_catalog(), usage_ledger=lambda: ledger),
        usage_receipts=ScheduledUsageReceipt(owner.submit, owner.schedule_completed),
    )
    state = AgentState(harness_inputs=[{"key": "test-input", "prompt": "do it"}])
    owner.start()
    with psycopg.connect(db_url) as blocker:
        _ = blocker.execute("LOCK TABLE llm_usage_ledger IN ACCESS EXCLUSIVE MODE")
        try:
            async with asyncio.timeout(10):
                if faulted:
                    with pytest.raises(FatalLLMStreamError, match="harness turn aborted"):
                        _ = await harness_turn_node(state, Runtime(context=ctx), config(agent_id))
                else:
                    _ = await harness_turn_node(state, Runtime(context=ctx), config(agent_id))
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
        _ = release.set()
        await owner.aclose(staging_timeout=2, delivery_timeout=1)

    pending = [decode_receipt(payload).tokens_in for _, payload in store.pending(after=0, limit=3)]
    with database.connect() as conn:
        posted = [row[0] for row in conn.execute("SELECT tokens_in FROM llm_usage_ledger")]
    assert set(pending + posted) == {100, 200}

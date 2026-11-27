import asyncio
from contextlib import ExitStack, closing
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from sqlite3 import connect
from threading import Event

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from base.api_contracts.usage import (
    ReceiptFallbackReporter,
    ReceiptSubmission,
    UsageLedger,
    UsageLedgerEntry,
)
from base.lm.usage.receipt import UsageReceiptRuntime, UsageReceiptStore, encode_receipt
from base.log.tests.log_records import LogRecords
from services.agent_runner.agent_host.daemon import close_process_owners


def _entry(cluster: str, call_id: str) -> UsageLedgerEntry:
    return UsageLedgerEntry(
        call_id=call_id,
        occurred_at=datetime(2026, 11, 27, tzinfo=UTC),
        agent_id=7,
        machine="test-host",
        cluster=cluster,
        source="codex",
        billing="subscription",
        usage_kind="agent",
        model="gpt-6.2-sol",
        requested_model=None,
        provider="openai",
        provider_request_id=None,
        calls=1,
        complete=True,
        tokens_in=100,
        tokens_out=10,
        tokens_reasoning=None,
        cache_read=None,
        cache_write_5m=None,
        cache_write_1h=None,
        cache_write_30m=None,
        cost_usd=Decimal("0.0001"),
        price_snapshot={"price_in": 1.0},
        price_source="catalog",
    )


def _offline(_entry: UsageLedgerEntry) -> bool:
    raise TimeoutError("ledger unavailable")


@pytest.mark.parametrize("expire", [False, True])
async def test_host_shutdown_drains_sqlite_write_or_logs_one_recoverable_fallback(
    tmp_path: Path, loguru_records: LogRecords, expire: bool
) -> None:
    cluster = tmp_path.name
    store = UsageReceiptStore(tmp_path, cluster, max_receipts=1, busy_seconds=12)
    entry = _entry(cluster, "harness:codex:7:session:1:turn:0")
    store.stage(_entry(cluster, "seed"))
    [(seq, _)] = store.pending(after=0, limit=1)
    if expire:
        store.reject(seq)
    else:
        store.acknowledge(seq)

    owner = UsageReceiptRuntime(store, UsageLedger(_offline, lambda: "test-host", cluster, str))
    entered = Event()

    def submit(
        frozen: UsageLedgerEntry, *, report_fallback: ReceiptFallbackReporter | None = None
    ) -> ReceiptSubmission:
        entered.set()
        return owner.submit(frozen, report_fallback=report_fallback)

    pool: AsyncConnectionPool[psycopg.AsyncConnection] = AsyncConnectionPool(open=False)
    pidfile = tmp_path / "agent-host.pid"
    pidfile.touch()
    with closing(connect(store.path)) as writer:
        _ = writer.execute("BEGIN IMMEDIATE")
        owner.start()
        staged = asyncio.wrap_future(owner.schedule_completed(entry, submit, lambda: None))
        try:
            assert await asyncio.to_thread(entered.wait, 2)
        finally:
            shutdown = asyncio.create_task(
                close_process_owners(
                    None, pool, pool, None, None, None, None, usage_receipts=owner, pidfile=pidfile
                )
            )
            if not expire:
                await asyncio.sleep(0)
                assert not shutdown.done() and not staged.done()
                assert pidfile.exists()
                writer.rollback()
            await asyncio.wait_for(shutdown, timeout=12)
            assert not pidfile.exists()
            if expire:
                assert not staged.done()
                writer.rollback()
            await asyncio.wait_for(staged, timeout=2)

    _assert_receipt_after_shutdown(store, entry, loguru_records, expire=expire)


def _assert_receipt_after_shutdown(
    store: UsageReceiptStore, entry: UsageLedgerEntry, loguru_records: LogRecords, *, expire: bool
) -> None:
    fallbacks = [
        extra
        for extra in loguru_records.extras()
        if extra.get("receipt_status") == "fallback_logged"
    ]
    if expire:
        [fallback] = fallbacks
        assert fallback["stage"] == "shutdown_drain"
        assert fallback["receipt_json"] == encode_receipt(entry).decode()
        assert not store.pending(after=0, limit=1)
    else:
        assert not fallbacks
        [(_, payload)] = store.pending(after=0, limit=1)
        assert payload == encode_receipt(entry)


async def test_already_reported_usage_is_recoverable_when_staging_admission_is_full(
    tmp_path: Path, loguru_records: LogRecords
) -> None:
    cluster = tmp_path.name
    entry = _entry(cluster, "harness:codex:7:session:1:turn:0")
    store = UsageReceiptStore(tmp_path, cluster)
    owner = UsageReceiptRuntime(store, UsageLedger(_offline, lambda: "test-host", cluster, str))
    owner.start()

    try:
        with ExitStack() as reservations:
            for _ in range(256):
                _ = reservations.enter_context(owner.reserve())
            observed: list[str] = []
            staged = owner.schedule_completed(
                entry, owner.submit, lambda: observed.append(entry.call_id)
            )
            assert staged.result() is None
            assert observed == [entry.call_id]
    finally:
        await owner.aclose(staging_timeout=1, delivery_timeout=1)
    [fallback] = [extra for extra in loguru_records.extras() if "receipt_json" in extra]
    assert fallback["stage"] == "staging_admission"
    assert fallback["receipt_status"] == "fallback_logged"
    assert fallback["receipt_json"] == encode_receipt(entry).decode()

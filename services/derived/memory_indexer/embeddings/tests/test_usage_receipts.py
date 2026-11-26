from __future__ import annotations

import asyncio
import sys
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from threading import Event

import pytest
from loguru import logger

from base.api_contracts.usage import (
    ReceiptSubmission,
    UsageLedger,
    UsageLedgerEntry,
    UsageLedgerRejectedError,
)
from base.db import Database
from base.db.usage_ledger import usage_writer
from base.host.proc import run_bounded
from base.lm.usage.receipt import (
    UsageReceiptDelivery,
    UsageReceiptStore,
    decode_receipt,
    encode_receipt,
)
from base.packages.plugin_providers import build_model_catalog
from services.derived.memory_indexer.embeddings.gemini import DIM, MODEL_ID
from services.derived.memory_indexer.embeddings.tests.test_embeddings import (
    AsyncClient,
    build_provider,
)

_NOW = datetime(2026, 8, 17, tzinfo=UTC)


def _ledger(write: Callable[[UsageLedgerEntry], bool], *, cluster: str = "cluster") -> UsageLedger:
    return UsageLedger(write, lambda: "runner", cluster, lambda: "api:fixture")


def _entry(call_id: str, cluster: str = "cluster") -> UsageLedgerEntry:
    return UsageLedgerEntry(
        call_id=call_id,
        occurred_at=_NOW,
        agent_id=None,
        machine="test",
        cluster=cluster,
        source=None,
        billing="api",
        usage_kind="embedding",
        model=MODEL_ID,
        requested_model=None,
        provider="google",
        provider_request_id=None,
        calls=1,
        complete=True,
        tokens_in=123,
        tokens_out=0,
        tokens_reasoning=0,
        cache_read=None,
        cache_write_5m=None,
        cache_write_1h=None,
        cache_write_30m=None,
        cost_usd=Decimal("0.000001"),
        price_snapshot={"price_miss": 0.01},
        price_source="live",
    )


@pytest.mark.parametrize("mode", ["batch", "async_query"])
@pytest.mark.parametrize("error_type", [TimeoutError, RuntimeError])
def test_billed_embedding_survives_ledger_outage(
    mode: str, error_type: type[Exception], tmp_path: Path, database: Database
) -> None:
    called, unblock, failure_reported = Event(), Event(), Event()

    def unavailable(entry: UsageLedgerEntry) -> bool:
        called.set()
        assert unblock.wait(5)
        raise error_type(entry.call_id)

    ledger = _ledger(unavailable)
    catalog = replace(build_model_catalog(home=tmp_path), usage_ledger=lambda: ledger)
    store = UsageReceiptStore(tmp_path, ledger.cluster)
    delivery = UsageReceiptDelivery(store, ledger)
    transport = AsyncClient(vectors=[[1.0] * DIM], prompt_token_count=123)
    provider = build_provider(catalog=catalog, transport=transport, submit_receipt=delivery.submit)
    sink = logger.add(
        lambda _: failure_reported.set(),
        filter=lambda record: record["extra"].get("call_id") == "api:fixture",
    )
    delivery.start()
    try:
        if mode == "batch":
            assert provider.embed_batch(["hello"]).shape == (1, DIM)
        else:
            assert asyncio.run(provider.embed_query_async("hello")).shape == (DIM,)
        assert called.wait(5)
        [(_, payload)] = store.pending(after=0, limit=2)
        unblock.set()
        assert failure_reported.wait(5)
    finally:
        unblock.set()
        logger.remove(sink)
        assert delivery.stop(timeout=5)

    original = decode_receipt(payload)
    _assert_frozen_price(original)
    _assert_replayed_once(store, database, original)
    assert len(transport.calls) == 1


def _assert_frozen_price(entry: UsageLedgerEntry) -> None:
    assert entry.call_id == "api:fixture"
    assert entry.tokens_in == 123
    assert isinstance(entry.cost_usd, Decimal)
    price_miss = entry.price_snapshot["price_miss"]
    assert isinstance(price_miss, float) and price_miss > 0
    assert entry.price_snapshot["priced_at"] == entry.occurred_at.isoformat()


def _replay_to_postgres(store: UsageReceiptStore, database: Database) -> None:
    committed = Event()
    writer = usage_writer(database.connect)

    def write(entry: UsageLedgerEntry) -> bool:
        inserted = writer(entry)
        committed.set()
        return inserted

    delivery = UsageReceiptDelivery(store, _ledger(write, cluster=store.cluster))
    delivery.start()
    try:
        assert committed.wait(5)
    finally:
        assert delivery.stop(timeout=5)
    assert store.pending(after=0, limit=2) == []


def _assert_replayed_once(
    store: UsageReceiptStore, database: Database, original: UsageLedgerEntry
) -> None:
    _replay_to_postgres(store, database)
    store.recover(encode_receipt(original))
    _replay_to_postgres(store, database)
    with database.connect() as conn:
        rows = conn.execute(
            "SELECT call_id, occurred_at, machine, cost_usd, price_snapshot, tokens_in "
            "FROM llm_usage_ledger"
        ).fetchall()
    assert rows == [
        (
            original.call_id,
            original.occurred_at,
            original.machine,
            original.cost_usd,
            original.price_snapshot,
            original.tokens_in,
        )
    ]


def test_cancel_after_response_with_saturated_executor(tmp_path: Path, database: Database) -> None:
    def unexpected_write(entry: UsageLedgerEntry) -> bool:
        raise AssertionError(entry.call_id)

    ledger = _ledger(unexpected_write)
    store = UsageReceiptStore(tmp_path, ledger.cluster)
    delivery = UsageReceiptDelivery(store, ledger)
    catalog = replace(build_model_catalog(home=tmp_path), usage_ledger=lambda: ledger)
    transport = AsyncClient(vectors=[[1.0] * DIM], prompt_token_count=123)

    def cancel_after_parse() -> datetime:
        task = asyncio.current_task()
        assert task is not None
        assert task.cancel()
        return _NOW

    provider = build_provider(
        catalog=catalog,
        transport=transport,
        now=cancel_after_parse,
        submit_receipt=delivery.submit,
    )

    async def exercise() -> None:
        release = Event()
        pool = ThreadPoolExecutor(max_workers=1)
        loop = asyncio.get_running_loop()
        loop.set_default_executor(pool)
        occupied = pool.submit(release.wait)
        try:
            task = asyncio.create_task(provider.embed_query_async("hello"))
            with pytest.raises(asyncio.CancelledError):
                await task
            assert not occupied.done()
            assert store.backlog().pending == 1
        finally:
            release.set()

    asyncio.run(exercise())
    assert len(transport.calls) == 1
    _replay_to_postgres(store, database)


def test_isolated_homes_skip_foreign_receipts_and_keep_healthy_rows(tmp_path: Path) -> None:
    other_home = tmp_path / "second"
    other_home.mkdir()
    first = UsageReceiptStore(tmp_path, "first")
    second = UsageReceiptStore(other_home, "second")
    for call_id in ("permanent", "unknown", "healthy"):
        first.stage(_entry(call_id, "first"))
    second.stage(_entry("foreign", "second"))
    with pytest.raises(ValueError, match="share ownership"):
        _ = UsageReceiptDelivery(second, _ledger(lambda _: True, cluster="first"))
    posted = Event()

    def write(entry: UsageLedgerEntry) -> bool:
        assert entry.cluster == "first"
        if entry.call_id == "permanent":
            raise UsageLedgerRejectedError("invalid row")
        if entry.call_id == "unknown":
            raise RuntimeError("unknown outcome")
        assert entry.call_id == "healthy"
        posted.set()
        return True

    delivery = UsageReceiptDelivery(
        first, _ledger(write, cluster="first"), poll_seconds=0.01, batch_size=1
    )
    delivery.start()
    try:
        assert posted.wait(5)
    finally:
        assert delivery.stop(timeout=5)

    assert first.backlog().total == 2
    assert first.backlog().pending == 1
    assert second.backlog().pending == 1


def test_running_consumer_recovers_receipt_from_hard_exit(tmp_path: Path) -> None:
    scanned, posted = Event(), Event()

    def write(entry: UsageLedgerEntry) -> bool:
        if entry.call_id == "existing":
            scanned.set()
        else:
            assert entry.call_id == "orphan"
            posted.set()
        return True

    store = UsageReceiptStore(tmp_path, "cluster")
    store.stage(_entry("existing"))
    delivery = UsageReceiptDelivery(store, _ledger(write), poll_seconds=0.01)
    delivery.start()
    try:
        assert scanned.wait(5)
        child = run_bounded(
            [sys.executable, str(Path(__file__).with_name("receipt_child.py")), str(tmp_path)],
            input=encode_receipt(_entry("orphan")),
            capture_output=True,
            timeout=15,
        )
        assert child.returncode == 0, child.stderr
        assert posted.wait(5)
    finally:
        assert delivery.stop(timeout=5)
    assert store.backlog().pending == 0


def test_capacity_and_disk_fallback_emit_recoverable_receipt(tmp_path: Path) -> None:
    store = UsageReceiptStore(tmp_path, "cluster", max_receipts=1)
    delivery = UsageReceiptDelivery(store, _ledger(lambda _: True))
    existing, overflow = _entry("existing"), _entry("overflow")
    assert delivery.submit(existing) == ReceiptSubmission.STAGED
    assert delivery.submit(existing) == ReceiptSubmission.STAGED
    messages: list[str] = []
    sink = logger.add(
        lambda message: messages.append(str(message.record["extra"]["receipt_json"])),
        filter=lambda record: "receipt_json" in record["extra"],
    )
    try:
        assert delivery.submit(overflow) == ReceiptSubmission.FALLBACK_LOGGED
        other_home = tmp_path / "missing"
        broken = UsageReceiptDelivery(
            UsageReceiptStore(other_home, "cluster"), _ledger(lambda _: True)
        )
        assert broken.submit(overflow) == ReceiptSubmission.FALLBACK_LOGGED
    finally:
        logger.remove(sink)

    assert messages == [encode_receipt(overflow).decode()] * 2
    [(seq, _)] = store.pending(after=0, limit=2)
    store.acknowledge(seq)
    store.recover(messages[0].encode())
    assert [decode_receipt(payload) for _, payload in store.pending(after=0, limit=2)] == [overflow]

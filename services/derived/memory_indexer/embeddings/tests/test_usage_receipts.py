from __future__ import annotations

import asyncio
import sys
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from functools import partial
from pathlib import Path
from sqlite3 import connect
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
    UsageReceiptStaging,
    UsageReceiptStore,
    decode_receipt,
    encode_receipt,
)
from base.packages.plugin_providers import build_model_catalog
from services.derived.memory_indexer.embeddings.base import EmbeddingAPIError
from services.derived.memory_indexer.embeddings.gemini import DIM, MODEL_ID, GeminiEmbeddingProvider
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
    staging = UsageReceiptStaging()
    provider = build_provider(
        catalog=catalog,
        transport=transport,
        submit_receipt=delivery.submit,
        reserve_receipt=staging.reserve,
    )
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
        assert staging.stop(timeout=5)
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


def test_cancel_after_response_with_sqlite_lock_and_saturated_executor(
    tmp_path: Path, database: Database
) -> None:
    ledger = _ledger(lambda _: False)
    store = UsageReceiptStore(tmp_path, ledger.cluster)
    store.stage(_entry("seed"))
    [(seed, _)] = store.pending(after=0, limit=2)
    store.acknowledge(seed)
    delivery = UsageReceiptDelivery(store, ledger)
    staging = UsageReceiptStaging()
    catalog = replace(build_model_catalog(home=tmp_path), usage_ledger=lambda: ledger)
    transport = AsyncClient(vectors=[[1.0] * DIM], prompt_token_count=123)
    entered = Event()

    def submit(entry: UsageLedgerEntry) -> ReceiptSubmission:
        entered.set()
        return delivery.submit(entry)

    def cancel_after_parse() -> datetime:
        task = asyncio.current_task()
        assert task is not None
        assert task.cancel()
        return _NOW

    provider = build_provider(
        catalog=catalog,
        transport=transport,
        now=cancel_after_parse,
        submit_receipt=submit,
        reserve_receipt=staging.reserve,
    )
    try:
        with connect(store.path) as locked:
            _ = locked.execute("BEGIN IMMEDIATE")
            asyncio.run(_cancel_with_saturated_executor(provider, entered))
    finally:
        assert staging.stop(timeout=5)
    assert entered.is_set()
    assert store.backlog().pending == 1
    assert len(transport.calls) == 1
    _replay_to_postgres(store, database)


async def _cancel_with_saturated_executor(
    provider: GeminiEmbeddingProvider, entered: Event
) -> None:
    release = Event()
    pool = ThreadPoolExecutor(max_workers=1)
    loop = asyncio.get_running_loop()
    loop.set_default_executor(pool)
    occupied = pool.submit(release.wait)
    heartbeat = asyncio.Event()
    try:
        task = asyncio.create_task(provider.embed_query_async("hello"))
        with pytest.raises(asyncio.CancelledError):
            await task
        async with asyncio.timeout(2):
            while not entered.is_set():
                await asyncio.sleep(0.001)
            _ = loop.call_later(0.005, heartbeat.set)
            await heartbeat.wait()
        assert not occupied.done()
    finally:
        release.set()


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


def test_eight_processes_stage_all_receipts_without_busy_fallback(tmp_path: Path) -> None:
    store = UsageReceiptStore(tmp_path, "cluster")
    child = str(Path(__file__).with_name("receipt_child.py"))
    payload = encode_receipt(_entry("unused"))
    invoke = partial(run_bounded, input=payload, capture_output=True, timeout=40)
    with ThreadPoolExecutor(max_workers=8) as pool:
        jobs = [
            pool.submit(invoke, [sys.executable, child, str(tmp_path), str(index)])
            for index in range(8)
        ]
        for job in jobs:
            result = job.result()
            assert result.returncode == 0, result.stderr
    assert store.backlog().pending == 800


def test_oversized_fallback_is_recovered_and_posted(tmp_path: Path, database: Database) -> None:
    store = UsageReceiptStore(tmp_path, "cluster")
    delivery = UsageReceiptDelivery(store, _ledger(lambda _: False))
    entry = replace(_entry("large"), price_snapshot={"snapshot": "x" * 8192})
    messages: list[str] = []
    sink = logger.add(
        lambda message: messages.append(str(message.record["extra"]["receipt_json"])),
        filter=lambda record: "receipt_json" in record["extra"],
    )
    try:
        assert delivery.submit(entry) == ReceiptSubmission.FALLBACK_LOGGED
    finally:
        logger.remove(sink)
    [fallback] = messages
    assert len(fallback.encode()) > store.max_payload_bytes
    store.recover(fallback.encode())
    _replay_to_postgres(store, database)
    with database.connect() as conn:
        rows = conn.execute("SELECT call_id, price_snapshot FROM llm_usage_ledger").fetchall()
    assert rows == [(entry.call_id, entry.price_snapshot)]


def test_staging_capacity_refuses_before_gemini_call() -> None:
    staging = UsageReceiptStaging(max_outstanding=1)
    transport = AsyncClient(vectors=[[1.0] * DIM], prompt_token_count=123)
    provider = build_provider(transport=transport, reserve_receipt=staging.reserve)
    try:
        with staging.reserve(), pytest.raises(EmbeddingAPIError, match="at capacity"):
            asyncio.run(provider.embed_query_async("hello"))
    finally:
        assert staging.stop(timeout=5)
    assert transport.calls == []

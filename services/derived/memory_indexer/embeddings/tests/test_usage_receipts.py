from __future__ import annotations

import asyncio
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from threading import Event

import pytest
from loguru import logger

from base.api_contracts.usage import UsageLedger, UsageLedgerEntry
from base.db import Database
from base.db.usage_ledger import usage_writer
from base.lm.usage.receipt import UsageReceiptDelivery, UsageReceiptStore
from base.packages.plugin_providers import build_model_catalog
from services.derived.memory_indexer.embeddings.gemini import DIM
from services.derived.memory_indexer.embeddings.tests.test_embeddings import (
    AsyncClient,
    build_provider,
)


@pytest.mark.parametrize("mode", ["batch", "async_query"])
@pytest.mark.parametrize("error_type", [TimeoutError, RuntimeError])
def test_billed_embedding_survives_ledger_outage(
    mode: str, error_type: type[Exception], tmp_path: Path, database: Database
) -> None:
    called = Event()
    unblock = Event()
    attempted: list[UsageLedgerEntry] = []
    failure_reported = Event()

    def unavailable(entry: UsageLedgerEntry) -> bool:
        attempted.append(entry)
        called.set()
        assert unblock.wait(5)
        raise error_type("ledger unavailable")

    ledger = UsageLedger(unavailable, lambda: "runner", "cluster", lambda: "api:fixture")
    catalog = replace(build_model_catalog(home=tmp_path), usage_ledger=lambda: ledger)
    store = UsageReceiptStore(tmp_path / "receipts")
    delivery = UsageReceiptDelivery(store, ledger)
    transport = AsyncClient(vectors=[[1.0] * DIM], prompt_token_count=123)
    provider = build_provider(catalog=catalog, transport=transport, submit_receipt=delivery.submit)
    sink = logger.add(
        lambda _: failure_reported.set(),
        filter=lambda record: (
            record["extra"].get("call_id") == "api:fixture"
            and record["message"].startswith("Usage receipt pending")
        ),
    )
    try:
        if mode == "batch":
            assert provider.embed_batch(["hello"]).shape == (1, DIM)
        else:
            assert asyncio.run(provider.embed_query_async("hello")).shape == (DIM,)
        assert called.wait(5)
        assert not unblock.is_set()
        assert len(transport.calls) == 1
        [receipt] = list(store.directory.iterdir())
        frozen = receipt.read_bytes()
        [original] = attempted
        unblock.set()
        assert failure_reported.wait(5)
    finally:
        unblock.set()
        logger.remove(sink)
        assert delivery.stop(timeout=5)

    replay = UsageReceiptStore(store.directory)
    _assert_frozen_price(original)
    _assert_replayed_once(replay, ledger, database, receipt, frozen, original)
    assert len(transport.calls) == 1


def _assert_frozen_price(entry: UsageLedgerEntry) -> None:
    assert entry.call_id == "api:fixture"
    assert entry.tokens_in == 123
    assert isinstance(entry.cost_usd, Decimal)
    price_miss = entry.price_snapshot["price_miss"]
    assert isinstance(price_miss, float) and price_miss > 0
    assert entry.price_snapshot["priced_at"] == entry.occurred_at.isoformat()


def _assert_replayed_once(
    replay: UsageReceiptStore,
    ledger: UsageLedger,
    database: Database,
    receipt: Path,
    frozen: bytes,
    original: UsageLedgerEntry,
) -> None:
    healthy_ledger = replace(ledger, write=usage_writer(database.connect))
    assert replay.replay(healthy_ledger)
    assert list(replay.directory.iterdir()) == []

    _ = receipt.write_bytes(frozen)
    assert replay.replay(healthy_ledger)
    assert replay.replay(healthy_ledger)
    assert list(replay.directory.iterdir()) == []
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

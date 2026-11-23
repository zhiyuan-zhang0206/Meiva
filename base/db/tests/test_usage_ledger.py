from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from base.api_contracts.usage import (
    UsageLedgerConflictError,
    UsageLedgerEntry,
    UsageLedgerWriteError,
)
from base.db import Database
from base.db.usage_ledger import usage_writer
from base.log.tests.log_records import LogRecords
from base.log.tests.log_records import loguru_records as loguru_records


def _entry() -> UsageLedgerEntry:
    return UsageLedgerEntry(
        call_id="api:test",
        occurred_at=datetime(2026, 11, 1, tzinfo=UTC),
        agent_id=None,
        machine="test",
        cluster="test",
        source="test",
        billing="api",
        usage_kind="decision",
        model="test",
        requested_model=None,
        provider=None,
        provider_request_id=None,
        calls=1,
        complete=True,
        tokens_in=100,
        tokens_out=0,
        tokens_reasoning=None,
        cache_read=None,
        cache_write_5m=None,
        cache_write_1h=None,
        cache_write_30m=None,
        cost_usd=Decimal("0.000067"),
        price_snapshot={"price_miss": 0.67},
        price_source="live",
    )


def test_usage_replays_once_and_conflicts_fail(
    database: Database, loguru_records: LogRecords
) -> None:
    write = usage_writer(lambda: database)
    entry = _entry()
    write(entry)
    write(entry)
    with pytest.raises(UsageLedgerConflictError):
        write(replace(entry, tokens_in=101))
    with database.connect() as conn:
        rows = conn.execute("SELECT tokens_in, cost_usd FROM llm_usage_ledger").fetchall()
    assert rows == [(100, Decimal("0.000067"))]
    [record] = loguru_records.events("llm_usage_ledger_write_failed")
    assert record["extra"]["reason"] == "conflict"
    assert record["extra"]["failures"] == 1


def test_usage_write_failure_is_reported_and_propagates(loguru_records: LogRecords) -> None:
    def disconnected() -> Database:
        raise ConnectionError("unreachable")

    with pytest.raises(UsageLedgerWriteError, match="commit failed"):
        usage_writer(disconnected)(_entry())
    [record] = loguru_records.events("llm_usage_ledger_write_failed")
    assert record["extra"]["reason"] == "database"
    assert record["extra"]["failures"] == 1

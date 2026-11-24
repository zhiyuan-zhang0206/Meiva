from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import psycopg
import pytest
from psycopg import sql
from psycopg.types.json import Jsonb

from services.upkeep.events_maintenance.ledger_repricing import (
    load_price_book,
    reprice_unpriced_ledger,
)
from services.upkeep.events_maintenance.ledger_repricing_plan import UnpricedRow, plan_repricing

_AT = datetime(2026, 10, 29, 12, tzinfo=UTC)


def _row(
    call_id: str,
    model: str = "gpt-6.1-sol",
    tokens_in: int | None = 10_000,
    cache_write_5m: int | None = None,
    cache_write_30m: int | None = None,
) -> UnpricedRow:
    return UnpricedRow(
        call_id, _AT, model, tokens_in, 100, 2_000, cache_write_5m, None, cache_write_30m
    )


def test_a_row_is_priced_at_its_own_time_with_the_declared_class() -> None:
    (priced,) = plan_repricing([_row("a", cache_write_30m=3_000)], load_price_book())
    assert priced.cost_usd == Decimal("0.0187")
    assert priced.price_snapshot["price_write_30m"] == "2.5"
    assert priced.price_snapshot["priced_at"] == _AT.isoformat()
    assert priced.price_snapshot["priced_model"] == "gpt-6.1-sol"


def test_a_row_the_catalog_still_cannot_price_stays_out_of_the_plan() -> None:
    rows = [
        _row("write-class-without-a-rate", cache_write_5m=3_000),
        _row("unknown-model", model="no-such-model"),
        _row("no-token-counts", tokens_in=None),
    ]
    assert plan_repricing(rows, load_price_book()) == []


def _insert(conn: psycopg.Connection, call_id: str, **columns: object) -> None:
    values = {
        "call_id": call_id,
        "occurred_at": _AT,
        "machine": "test",
        "cluster": "test",
        "billing": "api",
        "usage_kind": "agent",
        "model": "gpt-6.1-sol",
        "provider": "openai",
        "calls": 1,
        "complete": True,
        "tokens_in": 10_000,
        "tokens_out": 100,
        "cache_read": 2_000,
        "cache_write_30m": 3_000,
        "price_snapshot": Jsonb({}),
    } | columns
    conn.execute(
        sql.SQL("INSERT INTO llm_usage_ledger ({}) VALUES ({})").format(
            sql.SQL(", ").join(map(sql.Identifier, values)),
            sql.SQL(", ").join(sql.Placeholder() for _ in values),
        ),
        list(values.values()),
    )


def test_the_pass_prices_what_it_can_once_and_never_touches_a_priced_row(
    db_conn: psycopg.Connection,
) -> None:
    _insert(db_conn, "fixable")
    _insert(db_conn, "stuck", cache_write_30m=None, cache_write_5m=3_000)
    _insert(
        db_conn,
        "already-priced",
        cost_usd=Decimal("9.5"),
        price_source="live",
        price_snapshot=Jsonb({"price_miss": 1}),
    )
    db_conn.commit()
    prices = load_price_book()

    first = reprice_unpriced_ledger(db_conn, prices=prices, batch_size=1)
    assert (first.scanned, first.priced, first.still_unpriced) == (2, 1, 1)
    assert first.added_usd == Decimal("0.0187")

    second = reprice_unpriced_ledger(db_conn, prices=prices)
    assert (second.scanned, second.priced, second.still_unpriced) == (1, 0, 1)

    rows = {
        row[0]: row[1:]
        for row in db_conn.execute(
            "SELECT call_id, cost_usd, price_source, price_snapshot->>'priced_at' "
            "FROM llm_usage_ledger"
        ).fetchall()
    }
    assert rows["fixable"] == (Decimal("0.0187"), "historical_catalog", _AT.isoformat())
    assert rows["stuck"] == (None, None, None)
    assert rows["already-priced"] == (Decimal("9.5"), "live", None)


def test_the_table_lets_a_row_change_only_from_unpriced_to_priced(
    db_conn: psycopg.Connection,
) -> None:
    _insert(db_conn, "priced", cost_usd=Decimal("1"), price_source="live")
    _insert(db_conn, "unpriced")
    db_conn.commit()
    refused = (
        "UPDATE llm_usage_ledger SET cost_usd = 2 WHERE call_id = 'priced'",
        "UPDATE llm_usage_ledger SET cost_usd = NULL, price_source = NULL WHERE call_id = 'priced'",
        "UPDATE llm_usage_ledger SET tokens_in = 1, cost_usd = 2, price_source = 'live' "
        "WHERE call_id = 'unpriced'",
        "UPDATE llm_usage_ledger SET cache_write_30m = 1, cost_usd = 2, price_source = 'live' "
        "WHERE call_id = 'unpriced'",
    )
    for statement in refused:
        with pytest.raises(psycopg.errors.IntegrityConstraintViolation):
            db_conn.execute(statement)  # pyright: ignore[reportArgumentType]
        db_conn.rollback()
    db_conn.execute(
        "UPDATE llm_usage_ledger SET cost_usd = 2, price_source = 'live' WHERE call_id = 'unpriced'"
    )
    db_conn.commit()


_MIGRATION = next(
    Path(__file__).resolve().parents[4].glob("migrations/**/*_llm-usage-ledger-price-only.sql")
)


def test_the_migration_files_openai_writes_under_the_class_the_vendor_reports(
    db_conn: psycopg.Connection,
) -> None:
    _insert(db_conn, "openai-5m", cache_write_30m=None, cache_write_5m=3_000)
    _insert(db_conn, "openai-mixed", cache_write_30m=500, cache_write_5m=3_000)
    _insert(db_conn, "anthropic", model="claude-opus-5-5", provider="anthropic", cache_write_5m=7)
    _insert(db_conn, "openai-30m")
    _insert(db_conn, "openai-priced", cache_write_5m=9, cost_usd=Decimal("1"), price_source="live")
    db_conn.commit()

    for _ in range(2):
        db_conn.execute(sql.SQL(_MIGRATION.read_text()))  # pyright: ignore[reportArgumentType]
        db_conn.commit()

    rows = {
        row[0]: row[1:]
        for row in db_conn.execute(
            "SELECT call_id, cache_write_5m, cache_write_1h, cache_write_30m FROM llm_usage_ledger"
        ).fetchall()
    }
    assert rows == {
        "openai-5m": (None, None, 3_000),
        "openai-mixed": (None, None, 3_500),
        "anthropic": (7, None, 3_000),
        "openai-30m": (None, None, 3_000),
        "openai-priced": (9, None, 3_000),
    }

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import psycopg
from psycopg.types.json import Jsonb

from services.upkeep.events_maintenance.unpriced_backfill import load_price_book
from services.upkeep.events_maintenance.usage_ledger_backfill import backfill_usage_ledger


def test_import_preserves_original_cost_prices_historical_nulls_and_reruns(
    db_conn: psycopg.Connection,
) -> None:
    prices = load_price_book()
    start = datetime(2026, 11, 1, tzinfo=UTC)
    end = datetime(2026, 11, 2, tzinfo=UTC)
    attrs = {"calls": 2, "source": "codex", "in_total": 1_000, "out_total": 100}
    for uid, (at, payload) in enumerate(
        [
            (start, attrs | {"model": "deepseek-flash", "cost_usd": 1.5}),
            (start, attrs | {"model": "deepseek-flash"}),
            (start, attrs | {"model": "unknown", "complete": False}),
            (end, attrs | {"model": "deepseek-flash", "cost_usd": 1.5}),
        ]
    ):
        db_conn.execute(
            """INSERT INTO telemetry_events
               (event_uid, ts, machine, cluster, process, category, event_name, level, source, attributes)
               VALUES (%s, %s, 'test', 'test', 'test', 'telemetry', 'llm_usage', 'info', 'system', %s)""",
            (uid, at, Jsonb(payload)),
        )
    db_conn.commit()
    counts = backfill_usage_ledger(db_conn, start=start, end=end, batch_size=2, prices=prices)
    assert (counts.scanned, counts.inserted, counts.catalog_priced, counts.still_unpriced) == (
        3,
        3,
        1,
        1,
    )
    repeat = backfill_usage_ledger(db_conn, start=start, end=end, batch_size=1, prices=prices)
    assert (repeat.scanned, repeat.inserted) == (3, 0)
    rows = db_conn.execute(
        "SELECT billing, calls, complete, price_source, cost_usd FROM llm_usage_ledger ORDER BY call_id"
    ).fetchall()
    assert rows[0] == ("subscription", 2, True, "legacy", Decimal("1.5"))
    assert rows[1][:4] == ("subscription", 2, True, "historical_catalog")
    assert rows[1][4] > 0
    assert rows[2] == ("subscription", 2, False, None, None)


def test_recover_failed_write_with_source_time_and_exact_price(db_conn: psycopg.Connection) -> None:
    observed = datetime(2026, 11, 2, tzinfo=UTC)
    occurred = observed - timedelta(seconds=1)
    exact = Decimal("0.0000001234567890123456789")
    db_conn.execute(
        """INSERT INTO telemetry_events
           (event_uid, ts, machine, cluster, process, category, event_name, level, source, attributes)
           VALUES (12, %s, 'test', 'test', 'test', 'telemetry', 'llm_usage', 'info', 'system', %s)""",
        (
            observed,
            Jsonb(
                {
                    "call_id": "api:provider:deepseek:request-1",
                    "occurred_at": occurred.isoformat(),
                    "provider_request_id": "request-1",
                    "model": "deepseek-flash",
                    "in_total": 100,
                    "out_total": 10,
                    "cost_usd": float(exact),
                    "cost_usd_exact": str(exact),
                }
            ),
        ),
    )
    db_conn.commit()
    prices = load_price_book()
    end = observed + timedelta(days=1)
    for expected in (1, 0):
        counts = backfill_usage_ledger(
            db_conn, start=observed, end=end, batch_size=1, prices=prices
        )
        assert (counts.scanned, counts.inserted) == (1, expected)
    assert db_conn.execute(
        "SELECT occurred_at, provider_request_id, cost_usd FROM llm_usage_ledger"
    ).fetchone() == (occurred, "request-1", exact)

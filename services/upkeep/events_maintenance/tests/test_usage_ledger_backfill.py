from datetime import UTC, datetime
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

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import psycopg
from psycopg.types.json import Jsonb

from services.upkeep.events_maintenance.unpriced_backfill import load_price_book
from services.upkeep.events_maintenance.usage_ledger_backfill import (
    LiveUsageWriter,
    UsageReconcileReport,
    backfill_usage_ledger,
    reconcile_usage_ledger,
)


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


def test_reconcile_reads_both_sides_and_identifies_duplicate_and_live_reports(
    db_conn: psycopg.Connection,
) -> None:
    start = datetime(2026, 11, 1, tzinfo=UTC)
    end = start + timedelta(days=1)
    exact = Decimal("0.012345678901234567890123456789")
    api = {
        "model": "deepseek-flash",
        "call_id": "api:live",
        "cost_usd_exact": str(exact),
        "in_total": 90,
        "out_total": 9,
        "cache_read": 25,
        "cache_write_5m": 10,
        "reasoning": 3,
    }
    subscribed = {
        "model": "deepseek-flash",
        "source": "codex",
        "calls": 2,
        "in_total": 1_000,
        "out_total": 100,
    }
    for uid, at, attrs in (
        (1, start, api),
        (2, start, api),
        (3, start, subscribed),
        (4, start, subscribed | {"model": "unknown", "complete": False}),
        (5, end, api | {"call_id": "api:next-day"}),
    ):
        db_conn.execute(
            """INSERT INTO telemetry_events
               (event_uid, ts, machine, cluster, process, category, event_name, level, source, attributes)
               VALUES (%s, %s, 'test-runner', 'test', 'agent_host', 'telemetry', 'llm_usage',
                       'info', 'system', %s)""",
            (uid, at, Jsonb(attrs)),
        )
    db_conn.commit()
    prices = load_price_book()

    before = reconcile_usage_ledger(db_conn, start=start, end=end, batch_size=1, prices=prices)
    assert (before.source_events, before.repeated_source_reports, before.mismatched_groups) == (
        4,
        1,
        3,
    )
    assert all(row.ledger is None for row in before.groups)
    assert db_conn.execute("SELECT count(*) FROM llm_usage_ledger").fetchone() == (0,)
    db_conn.commit()

    imported = backfill_usage_ledger(db_conn, start=start, end=end, batch_size=1, prices=prices)
    assert (
        imported.scanned,
        imported.inserted,
        imported.catalog_priced,
        imported.still_unpriced,
    ) == (4, 3, 1, 1)
    db_conn.execute("UPDATE llm_usage_ledger SET price_source = 'live' WHERE call_id = 'api:live'")
    db_conn.commit()
    matched = reconcile_usage_ledger(db_conn, start=start, end=end, batch_size=1, prices=prices)
    assert (matched.source_events, matched.repeated_source_reports, matched.mismatched_groups) == (
        4,
        1,
        0,
    )
    assert matched.live_writers == [LiveUsageWriter("test-runner", "agent_host", 1)]
    _assert_reconciled_totals(matched, start.date(), exact)
    api_group = matched.groups[0].ledger

    db_conn.execute("UPDATE llm_usage_ledger SET model = 'ledger-only' WHERE call_id = 'api:live'")
    db_conn.commit()
    different = reconcile_usage_ledger(db_conn, start=start, end=end, batch_size=1, prices=prices)
    assert different.mismatched_groups == 2
    assert different.groups[0].source == api_group
    assert different.groups[0].ledger is None
    assert different.groups[2].source is None
    assert different.groups[2].ledger is not None
    assert different.groups[2].ledger.model == "ledger-only"


def _assert_reconciled_totals(matched: UsageReconcileReport, day: date, exact: Decimal) -> None:
    api_group, catalog_group, unpriced_group = [row.ledger for row in matched.groups]
    assert api_group is not None
    assert (api_group.day, api_group.billing, api_group.cost_usd) == (day, "api", exact)
    assert (
        api_group.calls,
        api_group.cache_read,
        api_group.cache_write_5m,
        api_group.tokens_reasoning,
    ) == (1, 25, 10, 3)
    assert catalog_group is not None
    assert catalog_group.billing == "subscription"
    assert catalog_group.cost_usd == catalog_group.historical_price_usd > 0
    assert unpriced_group is not None
    assert (unpriced_group.incomplete_reports, unpriced_group.unpriced_calls) == (1, 2)

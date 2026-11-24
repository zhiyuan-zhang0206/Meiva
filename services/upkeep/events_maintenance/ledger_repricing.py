# meiva:link base/lm/usage/README.md
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

import psycopg
from psycopg.rows import class_row
from psycopg.types.json import Jsonb

from base.lm.pricing import PriceBook
from base.lm.pricing.archive import load_archive
from services.upkeep.events_maintenance.ledger_repricing_plan import (
    PRICE_SOURCE,
    RowPrice,
    UnpricedRow,
    plan_repricing,
)

BATCH_SIZE = 1_000
MAX_BATCHES = 20

_SELECT = """
    SELECT call_id, occurred_at, model, tokens_in, tokens_out, cache_read,
           cache_write_5m, cache_write_1h, cache_write_30m
    FROM llm_usage_ledger
    WHERE cost_usd IS NULL AND usage_kind <> 'legacy_daily'
      AND (%(after_at)s::timestamptz IS NULL OR (occurred_at, call_id) > (%(after_at)s, %(after_id)s))
    ORDER BY occurred_at, call_id
    LIMIT %(batch_size)s
"""
_UPDATE = """
    UPDATE llm_usage_ledger AS ledger
    SET cost_usd = priced.cost_usd, price_snapshot = priced.snapshot, price_source = %(source)s
    FROM unnest(%(call_ids)s::text[], %(costs)s::numeric[], %(snapshots)s::jsonb[])
         AS priced(call_id, cost_usd, snapshot)
    WHERE ledger.call_id = priced.call_id AND ledger.cost_usd IS NULL
"""


def load_price_book() -> PriceBook:
    return PriceBook(load_archive(), {})


@dataclass(frozen=True, slots=True)
class RepricingCounts:
    scanned: int = 0
    priced: int = 0
    still_unpriced: int = 0
    added_usd: Decimal = Decimal(0)


def reprice_unpriced_ledger(
    conn: psycopg.Connection,
    *,
    prices: PriceBook,
    batch_size: int = BATCH_SIZE,
    max_batches: int = MAX_BATCHES,
) -> RepricingCounts:
    counts = RepricingCounts()
    after: UnpricedRow | None = None
    for _ in range(max_batches):
        with conn.transaction():
            conn.execute("SET TRANSACTION READ WRITE")
            conn.execute("SET LOCAL synchronous_commit TO on")
            rows = _read_page(conn, after, batch_size)
            if not rows:
                break
            planned = plan_repricing(rows, prices)
            written = _write(conn, planned)
        counts = RepricingCounts(
            counts.scanned + len(rows),
            counts.priced + written,
            counts.still_unpriced + len(rows) - len(planned),
            counts.added_usd + sum((row.cost_usd for row in planned), Decimal(0)),
        )
        after = rows[-1]
    return counts


def _read_page(
    conn: psycopg.Connection, after: UnpricedRow | None, batch_size: int
) -> list[UnpricedRow]:
    with conn.cursor(row_factory=class_row(UnpricedRow)) as cur:
        cur.execute(
            _SELECT,
            {
                "after_at": after.occurred_at if after is not None else None,
                "after_id": after.call_id if after is not None else None,
                "batch_size": batch_size,
            },
        )
        return cur.fetchall()


def _write(conn: psycopg.Connection, planned: list[RowPrice]) -> int:
    if not planned:
        return 0
    with conn.cursor() as cur:
        cur.execute(
            _UPDATE,
            {
                "source": PRICE_SOURCE,
                "call_ids": [row.call_id for row in planned],
                "costs": [row.cost_usd for row in planned],
                "snapshots": [Jsonb(row.price_snapshot) for row in planned],
            },
        )
        return max(cur.rowcount, 0)

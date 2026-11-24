# meiva:link base/lm/usage/README.md
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from base.agents.model.harness_models import resolve_harness_catalog_model
from base.lm.pricing import CacheTokens, PriceBook, quote

PRICE_SOURCE = "historical_catalog"

_RATE_KEYS = (
    "price_miss",
    "price_hit",
    "price_out",
    "price_write_5m",
    "price_write_1h",
    "price_write_30m",
)


@dataclass(frozen=True, slots=True)
class UnpricedRow:
    call_id: str
    occurred_at: datetime
    model: str
    tokens_in: int | None
    tokens_out: int | None
    cache_read: int | None
    cache_write_5m: int | None
    cache_write_1h: int | None
    cache_write_30m: int | None


@dataclass(frozen=True, slots=True)
class RowPrice:
    call_id: str
    cost_usd: Decimal
    price_snapshot: dict[str, str]


def plan_repricing(rows: Iterable[UnpricedRow], prices: PriceBook) -> list[RowPrice]:
    planned: list[RowPrice] = []
    for row in rows:
        priced = _price(row, prices)
        if priced is not None:
            planned.append(priced)
    return planned


def _price(row: UnpricedRow, prices: PriceBook) -> RowPrice | None:
    canonical = resolve_harness_catalog_model(row.model, prices)
    quoted = quote(
        canonical,
        row.tokens_in,
        row.tokens_out,
        CacheTokens(
            read=row.cache_read or 0,
            write_5m=row.cache_write_5m or 0,
            write_1h=row.cache_write_1h or 0,
            write_30m=row.cache_write_30m or 0,
        ),
        at=row.occurred_at,
        prices=prices,
    )
    if quoted is None or quoted.exact_usd is None:
        return None
    rates = quoted.rates
    values = (*rates.as_tuple(), rates.cache_write_5m, rates.cache_write_1h, rates.cache_write_30m)
    snapshot = {
        key: str(rate) for key, rate in zip(_RATE_KEYS, values, strict=True) if rate is not None
    }
    snapshot.update(priced_at=row.occurred_at.isoformat(), priced_model=canonical)
    return RowPrice(row.call_id, quoted.exact_usd, snapshot)

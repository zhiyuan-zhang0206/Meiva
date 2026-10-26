"""Versioned LLM pricing archive + deterministic quote selection.

``pricing_catalog_archive.json`` is the reviewed catalog source. It preserves
effective intervals, input-token tiers, recurring UTC windows, and provenance;
provider plugins mirror that complete shape for chat models, while catalog-only
services such as embeddings continue to price directly from the archive. Both
sources use the same parser and deterministic selection path.
``load_archive`` reads the archive.
``quote`` returns the selected rates with their cost atomically, and
``cost_usd`` remains the compatibility surface for existing readers. Schema v2
requires each catalog entry to identify its vendor; the cross-line contract
lives in ``pricing_catalog_schema.md``.

## Cache hit vs miss must be computed separately

Cache-hit input can be 30-120x cheaper than cache-miss input; in
multi-turn re-send same prefix the actual cache hit rate can be ~98%.
Using a 2-tuple (in, out) computes the entire `in` at cache-miss rate
and overestimates total cost by 30-50x — history: the same bug in
a batch run once reported $56.38 for a 30-case
SWE-bench, actual was $0.8; fix in PR #322 / commit 5c85b74. So the
legacy price tuple is `(cache_miss, cache_hit, out)` USD/M; optional cache-write
rates separately price creation by TTL without changing that tuple.

Every model entry carries its own official source URL and verification date.
Providers with machine-readable pricing can be reconciled by adapters; providers
without one remain reviewed catalog updates. Runtime never scrapes pricing pages.

## DeepSeek reasoning tokens

DeepSeek's reasoning tokens count into output — billed at the merged
output rate; no separation between reasoning vs answer
(usage_metadata reports separately but the underlying rate is the
same).

## Qwen: flat tiers only, and never derive a USD rate

A tier boundary here is an exact token count and the catalog validates gapless
coverage, but Alibaba's docs express Qwen boundaries only as `Input<=256k` /
`256k<Input<=1m` — the token value of "256K" is published nowhere. Guessing
262,144 against 256,000 misprices ~3x in the band between them, so a
length-tiered Qwen must not be registered off the docs website alone. All
three registered models sidestep this: an account's own `GET /api/v1/models`
reports `"range_name": "Default"` for each, i.e. a single flat tier with no
boundary to guess. That endpoint, not the docs prose, is the authority on
tiering.

**Do not convert CNY prices.** Alibaba publishes USD per model rather than
converting at one rate, and the implied rates diverge — qwen3.8-max's USD works
out to ~7.27 CNY/USD while qwen3.8-27b's is ~7.08. Deriving 27b from its CNY
figures at max's rate produced 0.4125/1.65/0.0825 against the published
0.424/1.696/0.085, a ~3% error baked permanently into every cost row. Read the
model page's own USD column.

Rates are also region-specific, and the endpoint is configurable
(`MEIVA_DASHSCOPE_BASE_URL`): these entries are Beijing, and Singapore prices the
same models differently (qwen3.8-max $2.00/$0.25/$6.00 against Beijing's
$1.65/$0.206/$4.951). Repointing the base URL at another region means
re-checking this catalog, not just the URL.

## qwen3.8-flash uses published Beijing USD

The Model Studio EN model page for qwen3.8-flash landed (checked 2026-09-02)
and publishes the Beijing USD column: $0.113 / $0.014 / $0.382. The catalog
now uses those figures rather than QwenCloud's $0.16 / $0.016 / $0.47, which
tracked the Singapore column and overstated Beijing cost. This is a published
USD column, not a CNY conversion, so the no-conversion rule above remains
intact.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterator, Mapping, Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from base.lm.pricing.catalog import (
    CacheTokens,
    CostQuote,
    ModelPrice,
    PricePeriod,
    PriceRates,
    PriceTier,
    PriceWindow,
    Rates,
    model_vendor,
    parse_catalog,
    plugin_price_provenance,
    rates_at,
)

if TYPE_CHECKING:
    from langchain_core.messages import AnyMessage

__all__ = [
    "CacheTokens",
    "CostQuote",
    "ModelPrice",
    "PriceBook",
    "PricePeriod",
    "PriceRates",
    "PriceTier",
    "PriceWindow",
    "Rates",
    "cost_usd",
    "load_archive",
    "model_vendor",
    "parse_catalog",
    "plugin_model_price",
    "plugin_price_provenance",
    "quote",
    "rates_at",
    "tally_tokens",
]


def load_archive() -> dict[str, ModelPrice]:
    path = Path(__file__).with_name("pricing_catalog_archive.json")
    raw: Any = json.loads(path.read_text(encoding="utf-8"))
    return parse_catalog(raw)


def plugin_model_price(
    model: str,
    price: PriceRates,
    *,
    plugin: str = "<unknown>",
) -> ModelPrice:
    if not price.source_url.startswith("https://"):
        raise ValueError(
            f"provider plugin {plugin!r}: source_url must be HTTPS, got {price.source_url!r}"
        )
    try:
        checked_at = date.fromisoformat(price.source_checked_at)
    except ValueError as e:
        raise ValueError(
            f"provider plugin {plugin!r}: source_checked_at must be YYYY-MM-DD, "
            f"got {price.source_checked_at!r}"
        ) from e
    if checked_at.isoformat() != price.source_checked_at:
        raise ValueError(
            f"provider plugin {plugin!r}: source_checked_at must be YYYY-MM-DD, "
            f"got {price.source_checked_at!r}"
        )
    for name, value in (
        ("cache_miss", price.cache_miss),
        ("cache_hit", price.cache_hit),
        ("output", price.output),
        ("cache_write_5m", price.cache_write_5m),
        ("cache_write_1h", price.cache_write_1h),
    ):
        if value is not None and (not math.isfinite(value) or value < 0):
            raise ValueError(
                f"provider plugin {plugin!r}: {model!r} price {name} must be finite and non-negative"
            )
    periods_any: Any = price.periods
    raw_periods: list[dict[str, Any]] = (
        [
            {
                "effective_from": period.effective_from,
                "effective_until": period.effective_until,
                "tiers": [
                    {
                        "input_tokens_min": tier.input_tokens_min,
                        "input_tokens_max": tier.input_tokens_max,
                        "rates": {
                            "input": tier.cache_miss,
                            "cache_read": tier.cache_hit,
                            "output": tier.output,
                            "cache_write_5m": getattr(tier, "cache_write_5m", None),
                            "cache_write_1h": getattr(tier, "cache_write_1h", None),
                        },
                        "utc_daily_overrides": [
                            {
                                "start": window.start,
                                "end": window.end,
                                "rates": {
                                    "input": window.cache_miss,
                                    "cache_read": window.cache_hit,
                                    "output": window.output,
                                    "cache_write_5m": getattr(window, "cache_write_5m", None),
                                    "cache_write_1h": getattr(window, "cache_write_1h", None),
                                },
                            }
                            for window in tier.windows
                        ],
                    }
                    for tier in period.tiers
                ],
            }
            for period in periods_any
        ]
        if periods_any
        else [
            {
                "effective_from": None,
                "effective_until": None,
                "tiers": [
                    {
                        "input_tokens_min": 0,
                        "input_tokens_max": None,
                        "rates": {
                            "input": price.cache_miss,
                            "cache_read": price.cache_hit,
                            "output": price.output,
                            "cache_write_5m": price.cache_write_5m,
                            "cache_write_1h": price.cache_write_1h,
                        },
                        "utc_daily_overrides": [],
                    }
                ],
            }
        ]
    )
    return ModelPrice.from_raw_periods(
        model,
        vendor=price.vendor,
        source_url=price.source_url,
        source_checked_at=checked_at,
        periods=raw_periods,
    )


class PriceBook:
    def __init__(self, archive: Mapping[str, ModelPrice], plugin: Mapping[str, ModelPrice]) -> None:
        self.plugin: Mapping[str, ModelPrice] = MappingProxyType(dict(plugin))
        self.archive: Mapping[str, ModelPrice] = MappingProxyType(
            {model: price for model, price in archive.items() if model not in plugin}
        )

    def __contains__(self, model: object) -> bool:
        return model in self.archive or model in self.plugin

    def __iter__(self) -> Iterator[str]:
        return iter([*self.archive, *self.plugin])

    def __len__(self) -> int:
        return len(self.archive) + len(self.plugin)

    def vendor(self, model: str) -> str | None:
        entry = self.archive.get(model)
        if entry is not None:
            return entry.vendor
        plugin_price = self.plugin.get(model)
        return None if plugin_price is None else plugin_price.vendor

    def provenance(self, model: str) -> tuple[str, str] | None:
        entry = self.plugin.get(model)
        if entry is None:
            return None
        return (entry.source_url, entry.source_checked_at.isoformat())

    def rates_at(
        self,
        model: str,
        at: datetime | None = None,
        input_tokens: int | None = None,
    ) -> Rates | None:
        if input_tokens is not None and input_tokens < 0:
            raise ValueError("input_tokens must be non-negative")
        instant = _aware_utc(at)
        entry = self.archive.get(model)
        if entry is None:
            entry = self.plugin.get(model)
        if entry is None:
            return None
        return entry.rates_at(instant, input_tokens)


def _aware_utc(instant: datetime | None) -> datetime:
    if instant is None:
        return datetime.now(UTC)
    if instant.utcoffset() is None:
        raise ValueError("pricing instant must be timezone-aware")
    return instant.astimezone(UTC)


def quote(
    model: str,
    tok_in: int | None,
    tok_out: int | None,
    tok_cached: int | CacheTokens | None = None,
    *,
    at: datetime | None = None,
    prices: PriceBook,
) -> CostQuote | None:
    """Price one call, with cache reads/writes included in total input.

    When `tok_cached=None`, fall back to 0 (treats all as cache miss,
    overestimate but safe); for callers with only old `(in, out)`
    data. Unknown tokens, models, or effective intervals return ``None``.
    """
    if tok_in is None or tok_out is None:
        return None
    if isinstance(tok_cached, CacheTokens):
        cache_read = tok_cached.read
        cache_write_5m = tok_cached.write_5m
        cache_write_1h = tok_cached.write_1h
    else:
        cache_read = 0 if tok_cached is None else tok_cached
        cache_write_5m = 0
        cache_write_1h = 0
    if (
        min(tok_in, tok_out, cache_read, cache_write_5m, cache_write_1h) < 0
        or cache_read + cache_write_5m + cache_write_1h > tok_in
    ):
        raise ValueError("token counts must be non-negative and cached tokens cannot exceed input")
    selected = rates_at(model, at, tok_in, prices=prices)
    if selected is None:
        return None
    if (cache_write_5m and selected.cache_write_5m is None) or (
        cache_write_1h and selected.cache_write_1h is None
    ):
        return None
    cache_miss = tok_in - cache_read - cache_write_5m - cache_write_1h
    cost = (
        cache_miss * selected.cache_miss
        + cache_read * selected.cache_hit
        + tok_out * selected.output
        + cache_write_5m * (selected.cache_write_5m or 0)
        + cache_write_1h * (selected.cache_write_1h or 0)
    ) / 1_000_000
    return CostQuote(cost_usd=cost, rates=selected)


def cost_usd(
    model: str,
    tok_in: int | None,
    tok_out: int | None,
    tok_cached: int | CacheTokens | None = None,
    *,
    at: datetime | None = None,
    prices: PriceBook,
) -> float | None:
    """Compatibility wrapper returning only ``quote(...).cost_usd``.

    The original four positional arguments retain their meaning. ``at`` is an
    optional keyword-only extension for deterministic historical/boundary
    pricing; omitting it selects the schedule at the current UTC instant.
    """
    priced = quote(
        model,
        tok_in,
        tok_out,
        tok_cached,
        at=at,
        prices=prices,
    )
    return priced.cost_usd if priced is not None else None


def tally_tokens(
    messages: Sequence[AnyMessage],
) -> tuple[int | None, int | None, int | None]:
    from langchain_core.messages import AIMessage

    tok_in = 0
    tok_out = 0
    tok_cached = 0
    seen = False
    for m in messages:
        if not isinstance(m, AIMessage):
            continue
        meta = m.usage_metadata
        if not meta:
            continue
        seen = True
        tok_in += meta["input_tokens"]
        tok_out += meta["output_tokens"]
        raw_details: Any = meta.get("input_token_details") or {}
        details: dict[str, int] = raw_details
        tok_cached += sum(
            int(value or 0)
            for key, value in details.items()
            if key == "cache_read" or key.endswith("_cache_read")
        )
    if not seen:
        return None, None, None
    return tok_in, tok_out, tok_cached

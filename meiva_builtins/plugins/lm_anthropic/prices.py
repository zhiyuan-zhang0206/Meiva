# meiva:note
# The price lattice Anthropic publishes for its chat models: one ``PriceRates`` row per served id,
# carrying the base tier, the effective periods and the cache write rates. ``provider.py`` imports
# ``PRICES``; the pricing gate reads these literal rows without executing the plugin.

from __future__ import annotations

from base.lm.provider_api import PricePeriod, PriceRates, PriceTier

PRICES = {
    "claude-sonnet-5": PriceRates(
        cache_miss=2.0,
        cache_hit=0.20,
        output=10.0,
        cache_write_5m=2.500,
        cache_write_1h=4.0,
        source_url="https://www.anthropic.com/pricing",
        source_checked_at="2026-10-08",
        vendor="anthropic",
    ),
    "claude-sonnet-5-5": PriceRates(
        cache_miss=2.0,
        cache_hit=0.20,
        output=10.0,
        cache_write_5m=2.500,
        cache_write_1h=4.0,
        source_url="https://platform.claude.com/docs/en/models/sonnet-5-5/overview",
        source_checked_at="2026-10-08",
        vendor="anthropic",
    ),
    "claude-haiku-5-5": PriceRates(
        cache_miss=0.10,
        cache_hit=0.01,
        output=0.50,
        cache_write_5m=0.125,
        cache_write_1h=0.20,
        source_url="https://platform.claude.com/docs/en/models/haiku-5-5/overview",
        source_checked_at="2026-10-08",
        vendor="anthropic",
        periods=(
            PricePeriod(
                effective_from=None,
                effective_until=None,
                # meiva:note
                # Above 100K input tokens, the higher rates price the whole call.
                tiers=(
                    PriceTier(
                        input_tokens_min=0,
                        input_tokens_max=100_000,
                        cache_miss="0.10",
                        cache_hit="0.01",
                        output="0.50",
                        cache_write_5m="0.125",
                        cache_write_1h="0.20",
                    ),
                    PriceTier(
                        input_tokens_min=100_001,
                        input_tokens_max=None,
                        cache_miss="0.50",
                        cache_hit="0.05",
                        output="2.50",
                        cache_write_5m="0.625",
                        cache_write_1h="1.0",
                    ),
                ),
            ),
        ),
    ),
    "claude-haiku-4-5-20251001": PriceRates(
        cache_miss=1.0,
        cache_hit=0.10,
        output=5.0,
        cache_write_5m=1.250,
        cache_write_1h=2.0,
        source_url="https://www.anthropic.com/pricing",
        source_checked_at="2026-10-08",
        vendor="anthropic",
    ),
    "claude-opus-5": PriceRates(
        cache_miss=5.0,
        cache_hit=0.50,
        output=25.0,
        cache_write_5m=6.250,
        cache_write_1h=10.0,
        source_url="https://www.anthropic.com/pricing",
        source_checked_at="2026-10-08",
        vendor="anthropic",
    ),
    "claude-opus-5-5": PriceRates(
        cache_miss=4.0,
        cache_hit=0.20,
        output=20.0,
        cache_write_5m=5.000,
        cache_write_1h=8.0,
        source_url="https://www.anthropic.com/pricing",
        source_checked_at="2026-10-08",
        vendor="anthropic",
    ),
    "claude-fable-5": PriceRates(
        cache_miss=10.0,
        cache_hit=1.0,
        output=50.0,
        cache_write_5m=12.500,
        cache_write_1h=20.0,
        source_url="https://www.anthropic.com/pricing",
        source_checked_at="2026-10-08",
        vendor="anthropic",
    ),
    "claude-fable-5-1": PriceRates(
        cache_miss=10.0,
        cache_hit=0.25,
        output=50.0,
        cache_write_5m=12.500,
        cache_write_1h=20.0,
        source_url="https://www.anthropic.com/pricing",
        source_checked_at="2026-10-08",
        vendor="anthropic",
    ),
}

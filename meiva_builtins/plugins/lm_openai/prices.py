# meiva:note
# The price lattice OpenAI publishes for its chat models: one ``PriceRates`` row per served
# standard id, carrying the base tier, the effective periods and the token tiers above the
# long-context threshold. ``provider.py`` imports ``PRICES``; the pricing gate reads these
# literal rows without executing the plugin.

from __future__ import annotations

from base.lm.provider_api import PricePeriod, PriceRates, PriceTier

PRICES = {
    "gpt-6-astra": PriceRates(
        cache_miss=10.0,
        cache_hit=1.0,
        output=50.0,
        source_url="https://developers.openai.com/api/docs/models/gpt-6-astra",
        source_checked_at="2026-09-06",
        vendor="openai",
        # meiva:note
        # Cache writes bill 1.25x uncached input ($12.50/M) — no cache-write
        # field exists, so that rate stays a note. The >272K input rule IS
        # modeled: 2x input and cache rates, 1.5x output, full request
        # (official model page).
        periods=(
            PricePeriod(
                effective_from=None,
                effective_until=None,
                tiers=(
                    PriceTier(
                        input_tokens_min=0,
                        input_tokens_max=272_000,
                        cache_miss="10.0",
                        cache_hit="1.0",
                        output="50.0",
                    ),
                    PriceTier(
                        input_tokens_min=272_001,
                        input_tokens_max=None,
                        cache_miss="20.0",
                        cache_hit="2.0",
                        output="75.0",
                    ),
                ),
            ),
        ),
    ),
    "gpt-5.6-sol": PriceRates(
        # meiva:note
        # Promotional pricing, valid at least through 2026-11-21 (official
        # model page, checked 2026-09-06). On promo expiry, restore the
        # standard rates — tier1 5.0 / 0.5 / 30.0, tier2 (>272K input)
        # 10.0 / 1.0 / 45.0 — here AND in
        # base/lm/pricing/pricing_catalog_archive.json; a deliberate manual
        # flip, not an automatic revert (405 ruling 2026-09-07).
        cache_miss=4.0,
        cache_hit=0.4,
        output=20.0,
        source_url="https://developers.openai.com/api/docs/models/gpt-5.6-sol",
        source_checked_at="2026-09-06",
        vendor="openai",
        periods=(
            PricePeriod(
                effective_from=None,
                effective_until=None,
                tiers=(
                    PriceTier(
                        input_tokens_min=0,
                        input_tokens_max=272_000,
                        cache_miss="4.0",
                        cache_hit="0.4",
                        output="20.0",
                    ),
                    PriceTier(
                        input_tokens_min=272_001,
                        input_tokens_max=None,
                        cache_miss="8.0",
                        cache_hit="0.8",
                        output="30.0",
                    ),
                ),
            ),
        ),
    ),
    "gpt-6-sol": PriceRates(
        cache_miss=2.0,
        cache_hit=0.20,
        output=10.0,
        source_url="https://developers.openai.com/api/docs/models/gpt-6-sol",
        source_checked_at="2026-09-25",
        vendor="openai",
        # meiva:note
        # Cache writes cost $2.50/M; the full request uses tier 2 above
        # 272K input tokens (official model page, checked 2026-09-25).
        periods=(
            PricePeriod(
                effective_from=None,
                effective_until=None,
                tiers=(
                    PriceTier(
                        input_tokens_min=0,
                        input_tokens_max=272_000,
                        cache_miss="2.0",
                        cache_hit="0.20",
                        output="10.0",
                    ),
                    PriceTier(
                        input_tokens_min=272_001,
                        input_tokens_max=None,
                        cache_miss="4.0",
                        cache_hit="0.40",
                        output="15.0",
                    ),
                ),
            ),
        ),
    ),
    "gpt-6.1-sol": PriceRates(
        cache_miss=2.0,
        cache_hit=0.10,
        output=10.0,
        source_url="https://developers.openai.com/api/docs/models/gpt-6.1-sol",
        source_checked_at="2026-09-30",
        vendor="openai",
        # meiva:note
        # Cache writes cost $2.50/M; requests over 272K input tokens
        # use the higher rates for the full request.
        periods=(
            PricePeriod(
                effective_from=None,
                effective_until=None,
                tiers=(
                    PriceTier(
                        input_tokens_min=0,
                        input_tokens_max=272_000,
                        cache_miss="2.0",
                        cache_hit="0.10",
                        output="10.0",
                    ),
                    PriceTier(
                        input_tokens_min=272_001,
                        input_tokens_max=None,
                        cache_miss="4.0",
                        cache_hit="0.20",
                        output="15.0",
                    ),
                ),
            ),
        ),
    ),
    "gpt-6.1-terra": PriceRates(
        cache_miss=1.0,
        cache_hit=0.05,
        output=5.0,
        source_url="https://developers.openai.com/api/docs/models/gpt-6.1-terra",
        source_checked_at="2026-10-28",
        vendor="openai",
        periods=(
            PricePeriod(
                effective_from=None,
                effective_until=None,
                tiers=(
                    PriceTier(
                        input_tokens_min=0,
                        input_tokens_max=272_000,
                        cache_miss="1.0",
                        cache_hit="0.05",
                        output="5.0",
                    ),
                    PriceTier(
                        input_tokens_min=272_001,
                        input_tokens_max=None,
                        cache_miss="2.0",
                        cache_hit="0.10",
                        output="7.5",
                    ),
                ),
            ),
        ),
    ),
    "gpt-5.6-terra": PriceRates(
        cache_miss=2.0,
        cache_hit=0.20,
        output=12.0,
        source_url="https://developers.openai.com/api/docs/models/gpt-5.6-terra",
        source_checked_at="2026-07-30",
        vendor="openai",
        periods=(
            PricePeriod(
                effective_from=None,
                effective_until=None,
                tiers=(
                    PriceTier(
                        input_tokens_min=0,
                        input_tokens_max=272_000,
                        cache_miss="2.0",
                        cache_hit="0.20",
                        output="12.0",
                    ),
                    PriceTier(
                        input_tokens_min=272_001,
                        input_tokens_max=None,
                        cache_miss="4.0",
                        cache_hit="0.40",
                        output="18.0",
                    ),
                ),
            ),
        ),
    ),
    "gpt-5.6-luna": PriceRates(
        cache_miss=0.20,
        cache_hit=0.02,
        output=1.20,
        source_url="https://developers.openai.com/api/docs/models/gpt-5.6-luna",
        source_checked_at="2026-07-30",
        vendor="openai",
        periods=(
            PricePeriod(
                effective_from=None,
                effective_until=None,
                tiers=(
                    PriceTier(
                        input_tokens_min=0,
                        input_tokens_max=272_000,
                        cache_miss="0.20",
                        cache_hit="0.02",
                        output="1.20",
                    ),
                    PriceTier(
                        input_tokens_min=272_001,
                        input_tokens_max=None,
                        cache_miss="0.40",
                        cache_hit="0.04",
                        output="1.80",
                    ),
                ),
            ),
        ),
    ),
    "gpt-6-luna": PriceRates(
        cache_miss=0.10,
        cache_hit=0.01,
        output=0.50,
        source_url="https://developers.openai.com/api/docs/models/gpt-6-luna",
        source_checked_at="2026-09-25",
        vendor="openai",
        # meiva:note
        # Cache writes cost $0.125/M; the full request uses tier 2 above
        # 272K input tokens (official model page, checked 2026-09-25).
        periods=(
            PricePeriod(
                effective_from=None,
                effective_until=None,
                tiers=(
                    PriceTier(
                        input_tokens_min=0,
                        input_tokens_max=272_000,
                        cache_miss="0.10",
                        cache_hit="0.01",
                        output="0.50",
                    ),
                    PriceTier(
                        input_tokens_min=272_001,
                        input_tokens_max=None,
                        cache_miss="0.20",
                        cache_hit="0.02",
                        output="0.75",
                    ),
                ),
            ),
        ),
    ),
}

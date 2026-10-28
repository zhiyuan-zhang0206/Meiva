# meiva:note
# OpenAI's separately billed Fast service: the price lattice and reference throughput that
# ``with_fast_variants`` turns into the ``<id>-fast`` services. The pricing gate reads these
# literal rows without executing the plugin.

from __future__ import annotations

from base.lm.provider_api import PricePeriod, PriceRates, PriceTier
from base.lm.registry import ReferenceTps

FAST_PRICES = {
    "gpt-6-astra": PriceRates(
        cache_miss=20,
        cache_hit=2,
        output=100,
        source_url="https://developers.openai.com/api/docs/pricing",
        source_checked_at="2026-10-07",
        vendor="openai",
        periods=(
            PricePeriod(
                effective_from=None,
                effective_until=None,
                tiers=(
                    PriceTier(
                        input_tokens_min=0,
                        input_tokens_max=272000,
                        cache_miss="20",
                        cache_hit="2",
                        output="100",
                    ),
                    PriceTier(
                        input_tokens_min=272001,
                        input_tokens_max=None,
                        cache_miss="40",
                        cache_hit="4",
                        output="150",
                    ),
                ),
            ),
        ),
    ),
    "gpt-6.1-sol": PriceRates(
        cache_miss=4,
        cache_hit=0.2,
        output=20,
        source_url="https://developers.openai.com/api/docs/pricing",
        source_checked_at="2026-10-07",
        vendor="openai",
        periods=(
            PricePeriod(
                effective_from=None,
                effective_until=None,
                tiers=(
                    PriceTier(
                        input_tokens_min=0,
                        input_tokens_max=272000,
                        cache_miss="4",
                        cache_hit="0.2",
                        output="20",
                    ),
                    PriceTier(
                        input_tokens_min=272001,
                        input_tokens_max=None,
                        cache_miss="8",
                        cache_hit="0.4",
                        output="30",
                    ),
                ),
            ),
        ),
    ),
    "gpt-6-sol": PriceRates(
        cache_miss=4,
        cache_hit=0.4,
        output=20,
        source_url="https://developers.openai.com/api/docs/pricing",
        source_checked_at="2026-10-07",
        vendor="openai",
        periods=(
            PricePeriod(
                effective_from=None,
                effective_until=None,
                tiers=(
                    PriceTier(
                        input_tokens_min=0,
                        input_tokens_max=272000,
                        cache_miss="4",
                        cache_hit="0.4",
                        output="20",
                    ),
                    PriceTier(
                        input_tokens_min=272001,
                        input_tokens_max=None,
                        cache_miss="8",
                        cache_hit="0.8",
                        output="30",
                    ),
                ),
            ),
        ),
    ),
    "gpt-6-luna": PriceRates(
        cache_miss=0.2,
        cache_hit=0.02,
        output=1,
        source_url="https://developers.openai.com/api/docs/pricing",
        source_checked_at="2026-10-07",
        vendor="openai",
        periods=(
            PricePeriod(
                effective_from=None,
                effective_until=None,
                tiers=(
                    PriceTier(
                        input_tokens_min=0,
                        input_tokens_max=272000,
                        cache_miss="0.2",
                        cache_hit="0.02",
                        output="1",
                    ),
                    PriceTier(
                        input_tokens_min=272001,
                        input_tokens_max=None,
                        cache_miss="0.4",
                        cache_hit="0.04",
                        output="1.5",
                    ),
                ),
            ),
        ),
    ),
    "gpt-6.1-terra": PriceRates(
        cache_miss=2,
        cache_hit=0.1,
        output=10,
        source_url="https://developers.openai.com/api/docs/pricing",
        source_checked_at="2026-10-28",
        vendor="openai",
        periods=(
            PricePeriod(
                effective_from=None,
                effective_until=None,
                tiers=(
                    PriceTier(
                        input_tokens_min=0,
                        input_tokens_max=272000,
                        cache_miss="2",
                        cache_hit="0.1",
                        output="10",
                    ),
                    PriceTier(
                        input_tokens_min=272001,
                        input_tokens_max=None,
                        cache_miss="4",
                        cache_hit="0.2",
                        output="15",
                    ),
                ),
            ),
        ),
    ),
    "gpt-5.6-sol": PriceRates(
        cache_miss=8,
        cache_hit=0.8,
        output=40,
        source_url="https://developers.openai.com/api/docs/pricing",
        source_checked_at="2026-10-07",
        vendor="openai",
        periods=(
            PricePeriod(
                effective_from=None,
                effective_until=None,
                tiers=(
                    PriceTier(
                        input_tokens_min=0,
                        input_tokens_max=272000,
                        cache_miss="8",
                        cache_hit="0.8",
                        output="40",
                    ),
                    PriceTier(
                        input_tokens_min=272001,
                        input_tokens_max=None,
                        cache_miss="16",
                        cache_hit="1.6",
                        output="60",
                    ),
                ),
            ),
        ),
    ),
    "gpt-5.6-terra": PriceRates(
        cache_miss=4,
        cache_hit=0.4,
        output=24,
        source_url="https://developers.openai.com/api/docs/pricing",
        source_checked_at="2026-10-07",
        vendor="openai",
        periods=(
            PricePeriod(
                effective_from=None,
                effective_until=None,
                tiers=(
                    PriceTier(
                        input_tokens_min=0,
                        input_tokens_max=272000,
                        cache_miss="4",
                        cache_hit="0.4",
                        output="24",
                    ),
                    PriceTier(
                        input_tokens_min=272001,
                        input_tokens_max=None,
                        cache_miss="8",
                        cache_hit="0.8",
                        output="36",
                    ),
                ),
            ),
        ),
    ),
    "gpt-5.6-luna": PriceRates(
        cache_miss=0.4,
        cache_hit=0.04,
        output=2.4,
        source_url="https://developers.openai.com/api/docs/pricing",
        source_checked_at="2026-10-07",
        vendor="openai",
        periods=(
            PricePeriod(
                effective_from=None,
                effective_until=None,
                tiers=(
                    PriceTier(
                        input_tokens_min=0,
                        input_tokens_max=272000,
                        cache_miss="0.4",
                        cache_hit="0.04",
                        output="2.4",
                    ),
                    PriceTier(
                        input_tokens_min=272001,
                        input_tokens_max=None,
                        cache_miss="0.8",
                        cache_hit="0.08",
                        output="3.6",
                    ),
                ),
            ),
        ),
    ),
}

FAST_TPS = {
    "gpt-5.6-sol": ReferenceTps(
        display=">80",
        source_url="https://openai.com/api-fast-mode/",
        source_checked_at="2026-10-07",
        note="Enterprise Fast latency SLA: 99%, calculated as p50 request latency per 5-minute window.",
    ),
    "gpt-5.6-terra": ReferenceTps(
        display=">70",
        source_url="https://openai.com/api-fast-mode/",
        source_checked_at="2026-10-07",
        note="Enterprise Fast latency SLA: 99%, calculated as p50 request latency per 5-minute window.",
    ),
    "gpt-5.6-luna": ReferenceTps(
        display=">100",
        source_url="https://openai.com/api-fast-mode/",
        source_checked_at="2026-10-07",
        note="Enterprise Fast latency SLA: 99%, calculated as p50 request latency per 5-minute window.",
    ),
}

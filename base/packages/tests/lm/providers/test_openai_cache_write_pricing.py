from typing import Any

import pytest
from langchain_core.messages import AIMessage
from langchain_openai.chat_models.base import _create_usage_metadata_responses

from base.lm.catalog import ModelCatalog
from base.lm.pricing import CacheTokens, quote
from base.lm.pricing.cache_writes import cache_write_tokens
from base.lm.usage import UsageOptions, log_usage_from_message


@pytest.mark.parametrize("tier", ["default", "priority", "fast"])
def test_openai_sdk_cache_creation_is_a_default_30_minute_write(tier: str) -> None:
    usage = _create_usage_metadata_responses(
        {
            "input_tokens": 1_000,
            "output_tokens": 50,
            "input_tokens_details": {"cached_tokens": 200, "cache_write_tokens": 700},
        },
        service_tier=tier,
    )
    assert cache_write_tokens(usage.get("input_token_details") or {}, default_ttl="30m") == (
        0,
        0,
        700,
    )


@pytest.mark.parametrize("tier,multiplier", [("default", 1), ("priority", 2), ("fast", 2)])
def test_openai_usage_snapshot_prices_writes_at_the_served_speed(
    model_catalog: ModelCatalog,
    loguru_records: list[dict[str, Any]],
    tier: str,
    multiplier: int,
) -> None:
    message = AIMessage(
        content="answer",
        response_metadata={"service_tier": tier},
        usage_metadata=_create_usage_metadata_responses(
            {
                "input_tokens": 39_205,
                "output_tokens": 205,
                "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 39_202},
            },
            service_tier=tier,
        ),
    )
    result = log_usage_from_message(
        message,
        "gpt-6.1-sol-fast",
        catalog=model_catalog,
        options=UsageOptions(stamp_message=True),
    )
    expected = (3 * 2 + 39_202 * 2.5 + 205 * 10) / 1_000_000 * multiplier
    assert result == pytest.approx((39_410, expected))
    snapshot = next(r["extra"] for r in loguru_records if r["extra"].get("event") == "llm_usage")
    assert snapshot["cache_write_30m"] == 39_202
    assert snapshot["cache_write_5m"] == 0
    assert snapshot["price_write_30m"] == 2.5 * multiplier
    assert snapshot["cost_usd"] == pytest.approx(expected)
    assert "unpriced" not in snapshot
    assert message.additional_kwargs["meiva_usage"]["cost_usd"] == snapshot["cost_usd"]


@pytest.mark.parametrize("tokens,write_rate,out_rate", [(272_000, 2.5, 10), (272_001, 5, 15)])
def test_openai_cache_writes_follow_the_full_request_context_tier(
    model_catalog: ModelCatalog, tokens: int, write_rate: float, out_rate: float
) -> None:
    priced = quote(
        "gpt-6.1-sol", tokens, 50, CacheTokens(write_30m=tokens), prices=model_catalog.prices
    )
    assert priced is not None
    assert priced.cost_usd == pytest.approx((tokens * write_rate + 50 * out_rate) / 1_000_000)


@pytest.mark.parametrize("writes", [-1, 101])
def test_openai_default_writes_must_be_a_valid_input_partition(
    model_catalog: ModelCatalog, writes: int
) -> None:
    with pytest.raises(ValueError):
        quote("gpt-6.1-sol", 100, 10, CacheTokens(write_30m=writes), prices=model_catalog.prices)


def test_other_providers_do_not_inherit_openai_write_rates(model_catalog: ModelCatalog) -> None:
    assert (
        quote("claude-opus-5-5", 100, 10, CacheTokens(write_30m=100), prices=model_catalog.prices)
        is None
    )

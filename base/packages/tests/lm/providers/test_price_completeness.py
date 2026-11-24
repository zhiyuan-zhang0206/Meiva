from collections.abc import Mapping

from base.lm.catalog import ModelCatalog
from base.lm.pricing import PriceRates, plugin_model_price
from base.lm.pricing.cache_writes import VENDOR_CACHE_WRITES, CacheWriteClass, default_write_ttl
from base.lm.pricing.catalog import ModelPrice, Rates

_CLASSES: tuple[CacheWriteClass, ...] = ("5m", "1h", "30m")


def _rate_sets(price: ModelPrice) -> list[Rates]:
    return [
        rates
        for period in price.periods
        for tier in period.tiers
        for rates in (tier.rates, *(window.rates for window in tier.utc_daily_overrides))
    ]


def _rate(rates: Rates, cls: CacheWriteClass) -> float | None:
    if cls == "5m":
        return rates.cache_write_5m
    return rates.cache_write_1h if cls == "1h" else rates.cache_write_30m


def _declared(rates: Rates) -> frozenset[CacheWriteClass]:
    return frozenset(cls for cls in _CLASSES if _rate(rates, cls) is not None)


def completeness_failures(
    prices: Mapping[str, ModelPrice], *, skip: frozenset[str] = frozenset()
) -> list[str]:
    failures: list[str] = []
    for model, price in sorted(prices.items()):
        if model in skip:
            continue
        if price.vendor is None or price.vendor not in VENDOR_CACHE_WRITES:
            failures.append(
                f"{model}: vendor {price.vendor!r} has no entry in VENDOR_CACHE_WRITES "
                "(base/lm/pricing/cache_writes.py)"
            )
            continue
        reportable = VENDOR_CACHE_WRITES[price.vendor]
        for rates in _rate_sets(price):
            declared = _declared(rates)
            if declared != reportable:
                failures.append(
                    f"{model}: declares write classes {sorted(declared)} but vendor "
                    f"{price.vendor!r} reports {sorted(reportable)}"
                )
                break
    return failures


def test_every_priced_chat_model_declares_the_write_classes_its_vendor_reports(
    model_catalog: ModelCatalog,
) -> None:
    prices = model_catalog.prices
    every_price = {**prices.archive, **prices.plugin}
    assert len(every_price) > 50
    assert completeness_failures(every_price, skip=frozenset(model_catalog.decision_models)) == []


def test_a_model_missing_a_reportable_class_fails() -> None:
    openai_without_30m = plugin_model_price(
        "gpt-test",
        PriceRates(
            cache_miss=2.0,
            cache_hit=0.1,
            output=10.0,
            cache_write_5m=2.5,
            source_url="https://example.com/p",
            source_checked_at="2026-11-24",
            vendor="openai",
        ),
    )
    (failure,) = completeness_failures({"gpt-test": openai_without_30m})
    assert "declares write classes ['5m'] but vendor 'openai' reports ['30m']" in failure


def test_zero_declares_a_class_reported_but_not_billed() -> None:
    free_writes = plugin_model_price(
        "gpt-test",
        PriceRates(
            cache_miss=2.0,
            cache_hit=0.1,
            output=10.0,
            cache_write_30m=0.0,
            source_url="https://example.com/p",
            source_checked_at="2026-11-24",
            vendor="openai",
        ),
    )
    assert completeness_failures({"gpt-test": free_writes}) == []


def test_a_new_vendor_must_declare_what_its_usage_reports() -> None:
    unknown = plugin_model_price(
        "newvendor-1",
        PriceRates(
            cache_miss=1.0,
            cache_hit=0.1,
            output=2.0,
            source_url="https://example.com/p",
            source_checked_at="2026-11-24",
            vendor="newvendor",
        ),
    )
    (failure,) = completeness_failures({"newvendor-1": unknown})
    assert "vendor 'newvendor' has no entry in VENDOR_CACHE_WRITES" in failure


def test_an_aggregate_creation_count_lands_in_a_class_its_vendor_reports() -> None:
    for vendor, classes in VENDOR_CACHE_WRITES.items():
        if classes:
            assert default_write_ttl(vendor) in classes

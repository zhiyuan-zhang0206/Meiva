from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time
from decimal import Decimal
from typing import NamedTuple, Protocol

from base.host.boundary.json_values import is_object_dict, is_object_list

__all__ = [
    "CacheTokens",
    "CostQuote",
    "ModelPrice",
    "PricePeriod",
    "PriceRates",
    "PriceTier",
    "PriceWindow",
    "Rates",
    "model_vendor",
    "parse_catalog",
    "plugin_price_provenance",
    "rates_at",
]


@dataclass(frozen=True, slots=True)
class CacheTokens:
    read: int = 0
    write_5m: int = 0
    write_1h: int = 0
    write_30m: int = 0


# meiva:note
# One recurring UTC rate override within a token tier.
class PriceWindow(NamedTuple):
    start: str
    end: str
    cache_miss: str | Decimal
    cache_hit: str | Decimal
    output: str | Decimal
    cache_write_5m: str | Decimal | None = None
    cache_write_1h: str | Decimal | None = None
    cache_write_30m: str | Decimal | None = None


# meiva:note
# One gapless input-token band and its optional daily overrides.
class PriceTier(NamedTuple):
    input_tokens_min: int
    input_tokens_max: int | None
    cache_miss: str | Decimal
    cache_hit: str | Decimal
    output: str | Decimal
    windows: tuple[PriceWindow, ...] = ()
    cache_write_5m: str | Decimal | None = None
    cache_write_1h: str | Decimal | None = None
    cache_write_30m: str | Decimal | None = None


# meiva:note
# One half-open effective interval containing all input-token tiers.
class PricePeriod(NamedTuple):
    effective_from: str | None
    effective_until: str | None
    tiers: tuple[PriceTier, ...]


# meiva:note
# One model's complete price and vendor declaration (USD per 1M tokens).
@dataclass(frozen=True)
class PriceRates:
    cache_miss: float
    cache_hit: float
    output: float
    source_url: str
    source_checked_at: str
    vendor: str | None = None
    periods: tuple[PricePeriod, ...] = ()
    cache_write_5m: float | None = field(default=None, kw_only=True)
    cache_write_1h: float | None = field(default=None, kw_only=True)
    cache_write_30m: float | None = field(default=None, kw_only=True)


# meiva:note
# Input, output, and optional cache-write USD per 1M tokens.
@dataclass(frozen=True)
class Rates:
    cache_miss: float
    cache_hit: float
    output: float
    cache_write_5m: float | None = field(default=None, kw_only=True)
    cache_write_1h: float | None = field(default=None, kw_only=True)
    cache_write_30m: float | None = field(default=None, kw_only=True)

    def as_tuple(self) -> tuple[float, float, float]:
        return (self.cache_miss, self.cache_hit, self.output)


# meiva:note
# One call's cost and the exact rates selected for it.
@dataclass(frozen=True)
class CostQuote:
    cost_usd: float
    rates: Rates


@dataclass(frozen=True)
class _UtcDailyOverride:
    start: time
    end: time
    rates: Rates

    def contains(self, instant: datetime) -> bool:
        current = instant.astimezone(UTC).time().replace(tzinfo=None)
        if self.start < self.end:
            return self.start <= current < self.end
        return current >= self.start or current < self.end


@dataclass(frozen=True)
class _TokenTier:
    input_tokens_min: int
    input_tokens_max: int | None
    rates: Rates
    utc_daily_overrides: tuple[_UtcDailyOverride, ...]

    def contains(self, input_tokens: int | None) -> bool:
        if input_tokens is None:
            return self.input_tokens_min == 0
        return input_tokens >= self.input_tokens_min and (
            self.input_tokens_max is None or input_tokens <= self.input_tokens_max
        )

    def rates_at(self, instant: datetime | None) -> Rates:
        if instant is not None:
            for override in self.utc_daily_overrides:
                if override.contains(instant):
                    return override.rates
        return self.rates


@dataclass(frozen=True)
class _EffectivePeriod:
    effective_from: datetime | None
    effective_until: datetime | None
    tiers: tuple[_TokenTier, ...]

    def contains(self, instant: datetime | None) -> bool:
        if instant is None:
            return self.effective_until is None
        return (self.effective_from is None or instant >= self.effective_from) and (
            self.effective_until is None or instant < self.effective_until
        )


# meiva:note
# Validated per-model pricing schedule across effective periods and input-token tiers.
@dataclass(frozen=True)
class ModelPrice:
    vendor: str | None
    source_url: str
    source_checked_at: date
    effective_time_note: str | None
    periods: tuple[_EffectivePeriod, ...]

    def rates_at(
        self, instant: datetime | None = None, input_tokens: int | None = None
    ) -> Rates | None:
        for period in self.periods:
            if not period.contains(instant):
                continue
            for tier in period.tiers:
                if tier.contains(input_tokens):
                    return tier.rates_at(instant)
            return None
        return None

    @classmethod
    def from_raw_periods(
        cls,
        model: str,
        *,
        vendor: str | None,
        source_url: str,
        source_checked_at: date,
        periods: Sequence[object],
    ) -> ModelPrice:
        return cls(
            vendor=vendor,
            source_url=source_url,
            source_checked_at=source_checked_at,
            effective_time_note=None,
            periods=_parse_periods(model, periods),
        )


# meiva:note
# The readers of the raw catalog JSON (or a plugin's price periods built in the same shape).
# A missing key is the `KeyError` of the indexing; a value of the wrong type is a `TypeError`
# naming what was expected.
def _object(value: object, what: str) -> Mapping[str, object]:
    if not is_object_dict(value):
        raise TypeError(f"pricing catalog {what} must be an object, got {type(value).__name__}")
    return value


def _array(value: object, what: str) -> list[object]:
    if not is_object_list(value):
        raise TypeError(f"pricing catalog {what} must be an array, got {type(value).__name__}")
    return value


def _text(value: object, what: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"pricing catalog {what} must be a string, got {type(value).__name__}")
    return value


def _whole(value: object, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"pricing catalog {what} must be an integer, got {type(value).__name__}")
    return value


def _number(value: object, what: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float | str | Decimal):
        raise TypeError(f"pricing catalog {what} must be a number, got {type(value).__name__}")
    return float(value)


def _instant(value: object, *, field: str) -> datetime | None:
    if value is None:
        return None
    text = _text(value, field)
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.utcoffset() is None:
        raise RuntimeError(f"pricing catalog {field} must carry a UTC offset: {text!r}")
    return parsed.astimezone(UTC)


def _clock(value: object) -> time:
    text = _text(value, "UTC daily clock")
    parsed = time.fromisoformat(text)
    if parsed.tzinfo is not None:
        raise RuntimeError(f"pricing catalog UTC daily clock must be offset-free: {text!r}")
    return parsed


def _rates(raw: Mapping[str, object]) -> Rates:
    writes = {
        key: _number(raw[key], key) if raw.get(key) is not None else None
        for key in ("cache_write_5m", "cache_write_1h", "cache_write_30m")
    }
    values = Rates(
        _number(raw["input"], "input"),
        _number(raw["cache_read"], "cache_read"),
        _number(raw["output"], "output"),
        **writes,
    )
    if any(
        not math.isfinite(value) or value < 0
        for value in (*values.as_tuple(), *writes.values())
        if value is not None
    ):
        raise RuntimeError(f"pricing catalog rates must be finite and non-negative: {raw!r}")
    return values


_DAY_MICROSECONDS = 24 * 60 * 60 * 1_000_000


def _clock_microseconds(value: time) -> int:
    return ((value.hour * 60 + value.minute) * 60 + value.second) * 1_000_000 + value.microsecond


def _window_segments(window: _UtcDailyOverride) -> tuple[tuple[int, int], ...]:
    if window.start == window.end:
        raise RuntimeError(
            "pricing catalog UTC daily window cannot cover zero or twenty-four hours"
        )
    start = _clock_microseconds(window.start)
    end = _clock_microseconds(window.end)
    if start < end:
        return ((start, end),)
    return ((0, end), (start, _DAY_MICROSECONDS))


def _validate_windows(model: str, windows: tuple[_UtcDailyOverride, ...]) -> None:
    segments: list[tuple[int, int]] = []
    for window in windows:
        for start, end in _window_segments(window):
            if any(start < seen_end and seen_start < end for seen_start, seen_end in segments):
                raise RuntimeError(f"pricing catalog has overlapping UTC windows for {model!r}")
            segments.append((start, end))


def _parse_tiers(model: str, raw_tiers: Sequence[object]) -> tuple[_TokenTier, ...]:
    tiers: list[_TokenTier] = []
    expected_min = 0
    for raw_tier in raw_tiers:
        raw = _object(raw_tier, "tier")
        lower = _whole(raw["input_tokens_min"], "input_tokens_min")
        upper_raw = raw["input_tokens_max"]
        upper = None if upper_raw is None else _whole(upper_raw, "input_tokens_max")
        if lower != expected_min or (upper is not None and upper < lower):
            raise RuntimeError(f"pricing catalog has a gap or overlap in tiers for {model!r}")
        windows = tuple(
            _UtcDailyOverride(
                start=_clock(item["start"]),
                end=_clock(item["end"]),
                rates=_rates(_object(item["rates"], "window rates")),
            )
            for item in (
                _object(raw_item, "window")
                for raw_item in _array(raw["utc_daily_overrides"], "utc_daily_overrides")
            )
        )
        _validate_windows(model, windows)
        tiers.append(
            _TokenTier(
                input_tokens_min=lower,
                input_tokens_max=upper,
                rates=_rates(_object(raw["rates"], "tier rates")),
                utc_daily_overrides=windows,
            )
        )
        expected_min = -1 if upper is None else upper + 1
    if not tiers or expected_min != -1:
        raise RuntimeError(f"pricing catalog tiers for {model!r} must cover all input sizes")
    return tuple(tiers)


def _parse_periods(model: str, raw_periods: Sequence[object]) -> tuple[_EffectivePeriod, ...]:
    periods: list[_EffectivePeriod] = []
    previous_until: datetime | None = None
    for index, raw_period in enumerate(raw_periods):
        raw = _object(raw_period, "period")
        start = _instant(raw["effective_from"], field="effective_from")
        end = _instant(raw["effective_until"], field="effective_until")
        if end is not None and start is not None and end <= start:
            raise RuntimeError(f"pricing catalog has an empty effective period for {model!r}")
        if index == 0 and start is not None:
            raise RuntimeError(f"pricing catalog must cover all history for {model!r}")
        if index > 0 and start != previous_until:
            raise RuntimeError(f"pricing catalog has a gap or overlap in periods for {model!r}")
        periods.append(
            _EffectivePeriod(
                effective_from=start,
                effective_until=end,
                tiers=_parse_tiers(model, _array(raw["tiers"], "tiers")),
            )
        )
        previous_until = end
    if not periods:
        raise RuntimeError(f"pricing catalog has no effective periods for {model!r}")
    if periods[-1].effective_until is not None:
        raise RuntimeError(f"pricing catalog must cover the present for {model!r}")
    return tuple(periods)


def parse_catalog(raw: Mapping[str, object]) -> dict[str, ModelPrice]:
    schema_version = raw["schema_version"]
    if (
        not isinstance(schema_version, int)
        or schema_version not in (1, 2)
        or raw["currency"] != "USD"
        or raw["unit_tokens"] != 1_000_000
    ):
        raise RuntimeError("unsupported pricing catalog schema, currency, or token unit")
    catalog_version = raw["catalog_version"]
    if not isinstance(catalog_version, str) or not catalog_version:
        raise RuntimeError("pricing catalog version must be a non-empty string")
    models = raw.get("models")
    if not is_object_dict(models) or not models:
        raise RuntimeError("pricing catalog models must be a non-empty mapping")
    return {
        model: _parse_model_price(model, _object(entry, "model entry"), schema_version)
        for model, entry in models.items()
    }


def _parse_model_price(
    model: str, entry_raw: Mapping[str, object], schema_version: int
) -> ModelPrice:
    vendor = entry_raw.get("vendor")
    if schema_version == 2 and (not isinstance(vendor, str) or not vendor.strip()):
        raise RuntimeError(f"pricing catalog vendor must be a non-empty string for {model!r}")
    if vendor is not None and not isinstance(vendor, str):
        raise RuntimeError(f"pricing catalog vendor must be a string for {model!r}")
    source_url = entry_raw["source_url"]
    if not isinstance(source_url, str) or not source_url.startswith("https://"):
        raise RuntimeError(f"pricing catalog source_url must be HTTPS for {model!r}")
    effective_time_note = entry_raw.get("effective_time_note")
    if effective_time_note is not None and (
        not isinstance(effective_time_note, str) or not effective_time_note
    ):
        raise RuntimeError(
            f"pricing catalog effective_time_note must be non-empty text for {model!r}"
        )
    return ModelPrice(
        vendor=vendor,
        source_url=source_url,
        source_checked_at=date.fromisoformat(
            _text(entry_raw["source_checked_at"], "source_checked_at")
        ),
        effective_time_note=effective_time_note,
        periods=_parse_periods(model, _array(entry_raw["periods"], "periods")),
    )


class _PriceLookup(Protocol):
    def vendor(self, model: str) -> str | None: ...

    def provenance(self, model: str) -> tuple[str, str] | None: ...

    def rates_at(
        self,
        model: str,
        at: datetime | None = None,
        input_tokens: int | None = None,
    ) -> Rates | None: ...


# meiva:note
# Vendor recorded by the catalog or a plugin, or None when unavailable.
def model_vendor(model: str, *, prices: _PriceLookup) -> str | None:
    return prices.vendor(model)


# meiva:note
# (source_url, source_checked_at) of a plugin-declared price, or None.
def plugin_price_provenance(model: str, *, prices: _PriceLookup) -> tuple[str, str] | None:
    return prices.provenance(model)


def rates_at(
    model: str,
    at: datetime | None = None,
    input_tokens: int | None = None,
    *,
    prices: _PriceLookup,
) -> Rates | None:
    return prices.rates_at(model, at, input_tokens)

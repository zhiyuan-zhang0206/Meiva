# meiva:note
# The plan an exec child hands its event pipeline: whether the downstream exporter holds records
# until exit, and how long a hold may age before it is brought up anyway. The child's entry
# builds it once from its environment; the pipeline and the exporter take it as a value and
# never read the environment.

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass

__all__ = [
    "CHILD_DEFER_MAX_AGE_DEFAULT_S",
    "ChildDeferPlan",
    "child_defer_plan",
    "parse_defer_flag",
    "parse_defer_seconds",
]

CHILD_DEFER_MAX_AGE_DEFAULT_S = 60.0
_CHILD_DEFER_ENV = "MEIVA_TELEMETRY_OTLP_CHILD_DEFER"
_CHILD_DEFER_MAX_AGE_ENV = "MEIVA_TELEMETRY_OTLP_CHILD_DEFER_MAX_AGE_S"
_TRUE_SPELLINGS = frozenset({"1", "true", "yes", "on"})
_FALSE_SPELLINGS = frozenset({"0", "false", "no", "off"})


@dataclass(frozen=True)
class ChildDeferPlan:
    enabled: bool
    max_age_s: float

    def __post_init__(self) -> None:
        if not math.isfinite(self.max_age_s) or self.max_age_s <= 0:
            raise ValueError(f"child defer max age must be positive and finite: {self.max_age_s!r}")


# meiva:note
# Parse a tristate child-defer boolean string; unrecognized values return ``default``.
def parse_defer_flag(raw: str | None, *, default: bool = True) -> bool:
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in _TRUE_SPELLINGS:
        return True
    if normalized in _FALSE_SPELLINGS:
        return False
    return default


# meiva:note
# Parse a positive finite max-age duration in seconds; invalid inputs return ``default``.
def parse_defer_seconds(raw: str | None, default: float = CHILD_DEFER_MAX_AGE_DEFAULT_S) -> float:
    if raw is None:
        return default
    normalized = raw.strip()
    if not normalized:
        return default
    try:
        value = float(normalized)
    except ValueError:
        return default
    return value if math.isfinite(value) and value > 0 else default


# meiva:note
# The deferral plan a child's environment names. The knobs are registered Settings fields
# (``MEIVA_TELEMETRY_OTLP_CHILD_DEFER[_MAX_AGE_S]``) so the ``.env`` surface and docs have one
# home, but a child reads them from its raw environment: going through the settings singleton
# would import the config chain the deferral exists to keep out of the child's life.
def child_defer_plan(environ: Mapping[str, str]) -> ChildDeferPlan:
    return ChildDeferPlan(
        enabled=parse_defer_flag(environ.get(_CHILD_DEFER_ENV)),
        max_age_s=parse_defer_seconds(environ.get(_CHILD_DEFER_MAX_AGE_ENV)),
    )

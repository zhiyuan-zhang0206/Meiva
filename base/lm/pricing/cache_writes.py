"""Normalize provider cache-creation usage without counting tokens twice."""

from collections.abc import Mapping
from typing import Literal


def _count(details: Mapping[str, object], key: str) -> int:
    count = details.get(key)
    if count is None:
        return 0
    if isinstance(count, bool) or not isinstance(count, int):
        raise TypeError("cache-write token counts must be integers")
    if count < 0:
        raise ValueError("cache-write token counts must be non-negative")
    return count


def cache_write_tokens(
    details: Mapping[str, object], *, default_ttl: Literal["5m", "30m"] = "5m"
) -> tuple[int, int, int]:
    # meiva:note
    # LangChain can clear the aggregate after copying the TTL breakdown; never
    # add both. OpenAI Responses uses a 30-minute default and may prefix its
    # aggregate with the served service tier.
    # meiva:link base/lm/pricing/docs/pricing.meiva.okf.md
    if default_ttl not in ("5m", "30m"):
        raise ValueError(f"unsupported default cache-write TTL: {default_ttl!r}")
    generic = sum(
        _count(details, key)
        for key in details
        if key == "cache_creation" or key.endswith("_cache_creation")
    )
    five = _count(details, "ephemeral_5m_input_tokens")
    hour = _count(details, "ephemeral_1h_input_tokens")
    if five + hour:
        if generic not in (0, five + hour):
            raise ValueError("cache-creation total disagrees with its TTL breakdown")
        return five, hour, 0
    return (0, 0, generic) if default_ttl == "30m" else (generic, 0, 0)

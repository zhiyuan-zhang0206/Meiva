"""The stats window vocabulary shared by the dashboard, inspector, and fleet
reads: the whitelisted `?hours=` values and their resolution to a duration.
"""

from datetime import timedelta
from enum import IntEnum


# meiva:note
# Whitelisted `?hours=` windows for stats, inspect, and fleet — 0 = last 5 minutes,
# 1h / 6h / 24h / 3d / 7d / 30d. An `IntEnum` (rather than `int` + range check) makes
# FastAPI 422 any non-whitelisted value instead of aggregating an arbitrary window, while
# coercing string query params that `Literal[1, 6, ...]` would reject.
# meiva:link gateway/cluster/docs/ops-surfaces.meiva.okf.md
class StatsWindowHours(IntEnum):
    M5 = 0
    H1 = 1
    H6 = 6
    H24 = 24
    D3 = 72
    D7 = 168
    D30 = 720


def window_delta(hours: StatsWindowHours) -> timedelta:
    """Resolve the `?hours=` wire value to its actual aggregation duration."""
    return timedelta(minutes=5) if hours == StatsWindowHours.M5 else timedelta(hours=int(hours))

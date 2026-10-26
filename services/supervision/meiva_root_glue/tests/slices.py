from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

from base.clock import SYSTEM_TIMERS, Clock, ClockConfig
from services.supervision.meiva_root_glue.diagnostic_probes import ProbeConfig

__all__ = ["probe_config", "system_clock"]


# meiva:note
# The real wall clock and timers, as the root builds them: a test that does not steer time passes
# this, one that does builds a `Clock` over `ManualTimers`.
def system_clock() -> Clock:
    return Clock(ClockConfig("UTC", "UTC", message_timestamp_weekday=False), timers=SYSTEM_TIMERS)


# meiva:note
# A diagnostics configuration for a local, non-remote cluster; a test that cares about a value
# passes it.
def probe_config(**overrides: Any) -> ProbeConfig:
    base = ProbeConfig(
        home=Path("/unused"),
        redis_url="redis://localhost:6380",
        db_url="postgresql://localhost/meiva_main",
        pgbouncer_enabled=False,
        observability_url="",
        observability_otlp_port=4318,
        gateway_health_url="http://127.0.0.1:8000/api/health",
        browser_cdp_port=9222,
        browser_profile=Path("/unused/chrome-profile"),
        browser_reach_timeout_s=5.0,
        browser_reach_probe_interval_s=60.0,
        loki_url="http://127.0.0.1:3100",
        environment={},
    )
    return replace(base, **overrides)

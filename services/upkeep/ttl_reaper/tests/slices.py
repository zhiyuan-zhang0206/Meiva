from __future__ import annotations

from dataclasses import replace
from typing import Any
from zoneinfo import ZoneInfo

from base.clock import SYSTEM_TIMERS, Clock, ClockConfig
from services.upkeep.ttl_reaper.config import TtlReaperConfig


def reaper_config(**overrides: Any) -> TtlReaperConfig:
    return replace(
        TtlReaperConfig(
            poll_interval_s=60.0,
            fire_log_cleanup_interval_s=86400.0,
            fire_log_retention_days=30,
            shell_kill_deadline_s=10.0,
            impersonation_event_abandon_grace_s=300,
            impersonation_event_report_interval_s=86400,
            zone=ZoneInfo("UTC"),
            clock=Clock(ClockConfig("UTC", "UTC", False), timers=SYSTEM_TIMERS),
        ),
        **overrides,
    )

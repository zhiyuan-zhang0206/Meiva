# meiva:note
# The values the reaper reads from configuration, taken once where the daemon starts.
#
# `daemon.ttl_reaper_config` builds this from the process's `ConfigBoot` and every loop and
# round receives the same frozen value, so no phase reads the live settings. `zone` is the
# cluster timezone the owner messages render times in; `None` is the "no zone configured"
# signal and renders the host zone, as `Clock.zone()` does.
from __future__ import annotations

from dataclasses import dataclass
from zoneinfo import ZoneInfo

from base.clock import Clock


@dataclass(frozen=True)
class TtlReaperConfig:
    poll_interval_s: float
    fire_log_cleanup_interval_s: float
    fire_log_retention_days: int
    shell_kill_deadline_s: float
    impersonation_event_abandon_grace_s: int
    impersonation_event_report_interval_s: int
    zone: ZoneInfo | None
    clock: Clock

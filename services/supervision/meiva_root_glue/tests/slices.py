from __future__ import annotations

import subprocess
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

from base.clock import SYSTEM_TIMERS, Clock, ClockConfig
from services.desktop.permissions_helper.launchd_job import LaunchdFacts
from services.supervision.meiva_root_glue import diagnostic_probes as probes
from services.supervision.meiva_root_glue.diagnostic_probes import ProbeConfig
from services.supervision.meiva_root_glue.diagnostics import Diagnostic

__all__ = ["checks_for", "probe_config", "system_clock"]

# meiva:note
# The account the diagnostics' launchd probe would read, and a runner that fails a test which lets
# that probe run: the roster tests build the checks without ever calling them.
LAUNCHD = LaunchdFacts(uid=501, platform="darwin", user_home=Path("/unused/user"))


def refuse_launchctl(
    cmd: Sequence[str], /, *, timeout: float, capture_output: bool = True
) -> subprocess.CompletedProcess[bytes]:
    del timeout, capture_output
    raise AssertionError(f"this test must not run {list(cmd)}")


# meiva:note
# `probes.build_diagnostics` for a roster test: the account's launchd probe is built, never run.
def checks_for(requested: set[str], **inputs: Any) -> list[Diagnostic]:
    return probes.build_diagnostics(requested, launchd=LAUNCHD, run=refuse_launchctl, **inputs)


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

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from base.host.proc import run_bounded

_PROBE = Path(__file__).with_name("child_env_bootstrap_tz.py")


def test_env_bootstrap_pins_utc_timezone() -> None:
    assert os.environ.get("TZ") == "UTC"
    if hasattr(time, "tzset"):
        assert time.timezone == 0
        assert "UTC" in time.tzname[0]


def test_env_bootstrap_forces_utc_even_when_parent_carries_non_utc() -> None:
    env = {**os.environ, "TZ": "America/New_York"}
    result = run_bounded(
        [sys.executable, str(_PROBE)],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report["incoming_tz"] == "America/New_York"
    assert report["bootstrapped_tz"] == "UTC"
    if hasattr(time, "tzset"):
        assert report["timezone"] == 0
        assert "UTC" in report["tzname"][0]

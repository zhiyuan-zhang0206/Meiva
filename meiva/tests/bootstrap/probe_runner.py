# meiva:note
# Run one scenario of `child_sdk_probes.py` in a fresh interpreter whose environment is the
# test's own: the launch markers (`MEIVA_AGENT_ID`, `MEIVA_CALLER_IDENTITY`) are dropped from the
# inherited environment (every `MEIVA_*` key and `VIRTUAL_ENV` with `clean=True`) and `env` adds
# what the scenario is about. The environment decides what a launched process makes of itself,
# so those scenarios are real children, not patches.
from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

from base.host.proc import run_bounded

__all__ = ["run_probe"]

_CHILD = Path(__file__).with_name("child_sdk_probes.py")
_LAUNCH_MARKERS = ("MEIVA_AGENT_ID", "MEIVA_CALLER_IDENTITY")


def run_probe(
    scenario: str,
    *args: str,
    env: Mapping[str, str] | None = None,
    clean: bool = False,
    timeout: float = 120,
) -> subprocess.CompletedProcess[str]:
    inherited = {
        k: v
        for k, v in os.environ.items()
        if k not in _LAUNCH_MARKERS
        and not (clean and (k.startswith("MEIVA_") or k == "VIRTUAL_ENV"))
    }
    return run_bounded(
        [sys.executable, "-I", "-B", str(_CHILD), scenario, *args],
        env={**inherited, **(env or {})},
        capture_output=True,
        text=True,
        timeout=timeout,
    )

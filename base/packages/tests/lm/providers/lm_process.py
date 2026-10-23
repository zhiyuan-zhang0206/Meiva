# meiva:note
# Child environments are closed before application imports; SDK globals never cross cases.
# meiva:link base/packages/tests/lm/docs/contracts.meiva.okf.md

import sys
from pathlib import Path

from base.host.proc import run_bounded


def run_billing(home: Path, scenario: str, path: str, cached: str) -> None:
    child = Path(__file__).with_name("_billing_child.py")
    result = run_bounded(
        [sys.executable, "-I", str(child), scenario, path, cached],
        timeout=60,
        capture_output=True,
        text=True,
        env={
            "HOME": str(home),
            "MEIVA_HOME": str(home),
            "MEIVA_CONFIG_FETCH": "skip",
            "MEIVA_CONFIG_BOOT": "eager",
            "MEIVA_PROCESS_PROFILE": "agent",
        },
    )
    assert result.returncode == 0, result.stdout + result.stderr


def run_fail_fast(home: Path, error: str, provider_cause: str) -> None:
    child = Path(__file__).with_name("_fail_fast_child.py")
    result = run_bounded(
        [sys.executable, "-I", str(child), error, provider_cause],
        timeout=60,
        capture_output=True,
        text=True,
        env={
            "HOME": str(home),
            "MEIVA_HOME": str(home),
            "MEIVA_CONFIG_FETCH": "skip",
            "MEIVA_CONFIG_BOOT": "eager",
            "MEIVA_PROCESS_PROFILE": "agent",
        },
    )
    assert result.returncode == 0, result.stdout + result.stderr

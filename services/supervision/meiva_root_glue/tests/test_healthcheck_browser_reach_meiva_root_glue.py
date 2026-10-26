from pathlib import Path

import pytest

from base.daemon.health import DaemonProbe
from services.supervision.healthchecks import browser_reach as hc
from services.supervision.meiva_root_glue import diagnostic_probes as probes


@pytest.mark.parametrize(
    "outcome,host_ok,expected",
    [
        ("ok", True, "alive"),
        ("timeout", True, "down"),
        ("error", True, "down"),
        ("timeout", False, "unavailable"),
        ("skip", True, "unavailable"),
    ],
)
def test_canary_distinguishes_browser_failure_from_unknown(
    outcome: str, host_ok: bool, expected: str
) -> None:
    assert (
        probes.browser_reach(
            url="https://example.invalid/health",
            port=9222,
            profile=Path("/unused/chrome-profile"),
            timeout_s=1.0,
            browser_verdict=DaemonProbe.up("owned"),
            canary_result=hc.CanaryResult(outcome, "browser evidence"),
            host_result=hc.HostResult(host_ok, "host evidence"),
        ).verdict.value
        == expected
    )

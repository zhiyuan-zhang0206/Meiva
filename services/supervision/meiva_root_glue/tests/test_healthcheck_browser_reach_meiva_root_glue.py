from pathlib import Path

import pytest

from base.daemon.health import DaemonProbe, ProbeVerdict
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


# meiva:note
# The canary answers *about the browser*, never "no verdict": its own timeout is a finding (DOWN
# when the host path answered, UNAVAILABLE when the host baseline failed too). That is why
# `meiva-ops-root-diagnostic-browser-reach` reads down/unavailable/port-taken over a 16-minute window
# — three samples at its 300s cadence — and excludes INCONCLUSIVE entirely. A diagnostic that could
# answer without a verdict would need a no-verdict window matched to that cadence, so the vocabulary
# is pinned here rather than left to a comment (2026-10-27, task #5656).
@pytest.mark.parametrize("outcome", ["ok", "timeout", "error", "skip"])
@pytest.mark.parametrize("host_ok", [True, False])
def test_the_browser_canary_never_answers_without_a_verdict(outcome: str, host_ok: bool) -> None:
    probe = probes.browser_reach(
        url="https://example.invalid/health",
        port=9222,
        profile=Path("/unused/chrome-profile"),
        timeout_s=1.0,
        browser_verdict=DaemonProbe.up("owned"),
        canary_result=hc.CanaryResult(outcome, "browser evidence"),
        host_result=hc.HostResult(host_ok, "host evidence"),
    )

    assert probe.verdict is not ProbeVerdict.INCONCLUSIVE


# meiva:note
# The other two ways the same check answers: a missing browser prerequisite, and no configured URL.
# Both are UNAVAILABLE, so the reachability rule's 16-minute window still reads them.
def test_the_browser_canary_prerequisites_answer_without_a_verdict_of_their_own() -> None:
    blocked = probes.browser_reach(
        url="https://example.invalid/health",
        port=9222,
        profile=Path("/unused/chrome-profile"),
        timeout_s=1.0,
        browser_verdict=DaemonProbe.down("no browser unit"),
    )
    unconfigured = probes.browser_reach(
        url=" ",
        port=9222,
        profile=Path("/unused/chrome-profile"),
        timeout_s=1.0,
    )

    assert [probe.verdict for probe in (blocked, unconfigured)] == [
        ProbeVerdict.UNAVAILABLE,
        ProbeVerdict.UNAVAILABLE,
    ]

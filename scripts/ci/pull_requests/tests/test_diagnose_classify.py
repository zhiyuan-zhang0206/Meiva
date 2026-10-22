from __future__ import annotations

from scripts.ci.pull_requests.diagnose import classify_check

# meiva:note
# These three strings are the ones a real 2026-10-22 run printed: the held-lock line, the
# cache verdict that used to read as a test failure, and a genuine test assertion. The
# classification is operator-facing, so what it says has to match what happened — a runner
# whose mirror is down must never read like a red test.
# meiva:link scripts/ci/pull_requests/docs/diagnostics.meiva.okf.md


def test_a_held_dpkg_lock_reads_as_infrastructure() -> None:
    log = (
        "apt lock: /var/lib/dpkg/lock-frontend is still held after 120.0s by pid(s) 5340 — "
        "that is a held lock, not a package or network failure"
    )

    classification, action = classify_check("Install Playwright system deps", log)

    assert classification.startswith("infrastructure:")
    assert "rerun" in action


def test_the_cache_verdict_from_a_lock_collision_reads_as_infrastructure() -> None:
    log = (
        "E: Could not get lock /var/lib/dpkg/lock-frontend. It is held by process 5340 (apt-get)\n"
        "::error::the cached archives cannot satisfy the system deps"
    )

    classification, _ = classify_check("Install Playwright system deps", log)

    assert classification.startswith("infrastructure:")


def test_a_real_test_failure_is_not_classified_as_infrastructure() -> None:
    classification, _ = classify_check(
        "e2e (Playwright happy path)", "AssertionError: expected 200, got 500"
    )

    assert not classification.startswith("infrastructure:")

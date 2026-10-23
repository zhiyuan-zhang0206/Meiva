# meiva:note
# Which authentication rejections are noise, and when a repeat earns a warning: values in.

from __future__ import annotations

import pytest

from gateway.http.auth.rejection_policy import (
    WARN_COOLDOWN_S,
    RejectionVerdict,
    idle_keys,
    is_expected_noise,
    rejection_verdict,
)


@pytest.mark.parametrize(
    ("path", "user_agent", "noise"),
    [
        ("/api/alerts/stream", "stale-browser", True),
        ("/api/agents/1/events/stream", "-", True),
        ("/api/system", "-", True),
        ("/api/agents/1/system", "-", True),
        ("/api/system/all", "-", True),
        ("/api/agents", "Mozilla/5.0 (Macintosh) Chrome/153", True),
        ("/api/agents", "curl/8.1", False),
        ("/api/alerts", "-", False),
    ],
)
def test_stale_sse_reconnects_and_browser_tabs_are_noise(
    path: str, user_agent: str, noise: bool
) -> None:
    assert is_expected_noise(path, user_agent) is noise


def test_a_key_never_warned_warns_first() -> None:
    verdict = rejection_verdict(now=100.0, last_warn=None, suppressed=0)
    assert verdict == RejectionVerdict("first", 0)
    assert verdict.warns


def test_repeats_inside_the_cooldown_are_counted_and_stay_quiet() -> None:
    verdict = rejection_verdict(now=100.0 + WARN_COOLDOWN_S - 1, last_warn=100.0, suppressed=2)
    assert verdict == RejectionVerdict("suppressed", 3)
    assert not verdict.warns


def test_the_first_repeat_after_the_cooldown_warns_with_the_hidden_count() -> None:
    verdict = rejection_verdict(now=100.0 + WARN_COOLDOWN_S, last_warn=100.0, suppressed=4)
    assert verdict == RejectionVerdict("reopened", 4)
    assert verdict.warns


def test_only_keys_idle_for_two_windows_are_forgotten() -> None:
    now = 10_000.0
    last_warn = {
        ("a", "/idle"): now - 2 * WARN_COOLDOWN_S - 1,
        ("b", "/recent"): now - 2 * WARN_COOLDOWN_S,
        ("c", "/fresh"): now,
    }
    assert idle_keys(last_warn, now) == (("a", "/idle"),)

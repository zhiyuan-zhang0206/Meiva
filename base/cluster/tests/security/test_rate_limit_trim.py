from __future__ import annotations

import pytest

from base.cluster.rate_limit import Entry, LoginRateLimitConfig, LoginRateLimiter


def _limiter(*, max_failures: int = 5, lockout_seconds: float = 900.0, max_entries: int = 10_000):
    return LoginRateLimiter(
        LoginRateLimitConfig(
            max_failures=max_failures, lockout_seconds=lockout_seconds, max_entries=max_entries
        )
    )


def test_sweep_trims_oldest_active_entries_over_cap() -> None:
    limiter = _limiter(max_entries=5)
    now = 1000.0
    for i in range(8):
        ip = f"10.0.0.{i}"
        limiter._entries[ip] = Entry(failures=4, locked_until=0.0, last_failure_at=now - (8 - i))
    assert len(limiter._entries) == 8
    limiter._sweep(now, 900.0)
    remaining = sorted(limiter._entries)
    assert len(remaining) == 5
    assert "10.0.0.0" not in remaining and "10.0.0.1" not in remaining
    assert "10.0.0.2" not in remaining
    assert "10.0.0.7" in remaining


@pytest.mark.parametrize("cap", [0, -1])
def test_a_cap_that_drops_every_entry_is_refused(cap: int) -> None:
    with pytest.raises(ValueError, match="max_entries"):
        LoginRateLimitConfig(max_failures=5, lockout_seconds=900.0, max_entries=cap)


@pytest.mark.parametrize(("failures", "window"), [(0, 900.0), (5, 0.0), (5, -1.0)])
def test_a_policy_that_can_never_lock_or_never_unlocks_is_refused(
    failures: int, window: float
) -> None:
    with pytest.raises(ValueError, match=r"max_failures|lockout_seconds"):
        LoginRateLimitConfig(max_failures=failures, lockout_seconds=window)


def test_sweep_stale_removed_first() -> None:
    limiter = _limiter()
    now = 1000.0
    limiter._entries["stale-ip"] = Entry(failures=1, locked_until=0.0, last_failure_at=now - 99999)
    limiter._entries["active-ip"] = Entry(failures=1, locked_until=0.0, last_failure_at=now)
    limiter._sweep(now, 900.0)
    assert "stale-ip" not in limiter._entries
    assert "active-ip" in limiter._entries


def test_the_policy_and_the_epoch_second_of_each_call_decide_the_lockout() -> None:
    limiter = _limiter(max_failures=2, lockout_seconds=17.5)
    limiter.record_failure("ip", now=1000.0)
    assert limiter.lockout_remaining("ip", now=1000.0) == 0
    limiter.record_failure("ip", now=1000.0)
    assert limiter.lockout_remaining("ip", now=1000.0) == 18
    assert limiter.lockout_remaining("ip", now=1010.0) == 8
    assert limiter.lockout_remaining("ip", now=1018.0) == 0
    limiter.record_success("ip")
    assert limiter.lockout_remaining("ip", now=1000.0) == 0


def test_limiters_with_different_policies_are_independent() -> None:
    left = _limiter(max_failures=2, lockout_seconds=100.0)
    right = _limiter(max_failures=3, lockout_seconds=30.0)
    for limiter in (left, right):
        limiter.record_failure("ip", now=1000.0)
        limiter.record_failure("ip", now=1000.0)
    assert left.lockout_remaining("ip", now=1000.0) == 100
    assert right.lockout_remaining("ip", now=1000.0) == 0
    right.record_failure("ip", now=1000.0)
    assert right.lockout_remaining("ip", now=1000.0) == 30
    assert left.lockout_remaining("ip", now=1012.0) == 88
    assert right.lockout_remaining("ip", now=1012.0) == 18

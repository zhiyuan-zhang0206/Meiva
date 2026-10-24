"""In-memory per-IP login failure rate limiting for the gateway.

The login endpoint is the gateway's only unauthenticated credential check,
so it is the one surface an online password guesser can hit without a
session. This module locks an IP out for a fixed window after a run of
consecutive failures.

State lives in a process-local dict — the gateway runs as a single uvicorn
process, so that dict IS the whole cluster. A gateway restart drops every
lockout and failure counter; acceptable here because brute force is a slow
online attack and a restart mid-attack merely re-arms the counter. No Redis
dependency on purpose: the point is to keep this small and obvious.

Preconditions (audit round-2 security P2 — break these and the limiter
silently degrades): single-process gateway, and NO reverse proxy in front —
the key is `request.client.host`, which behind a proxy is the proxy's
address (one user tripping the lockout would lock everyone) and the
failure counters would be shared across every proxied user. Both hold
today by deployment shape; re-check before adding a proxy or a second
gateway worker.

Policy (the threshold and the window are cluster config —
``gateway.login_max_failures`` / ``gateway.login_lockout_seconds``; those
fields carry the reasoning for their defaults, while the memory cap below is
a protective constant):

- ``gateway.login_max_failures`` consecutive failed logins per IP lock the IP
  for ``gateway.login_lockout_seconds`` — defaults 5 / 900 (15 minutes).
- A successful login resets the IP's failure counter.
- While locked, the login endpoint returns 429 + ``Retry-After`` instead of
  401: 401 reads as "wrong password" and invites the retry loop the lockout
  exists to stop, while 429 + Retry-After states plainly that the credential
  was not evaluated and when to retry.
- Lockout expiry resets the streak: the first attempt after the window
  starts a fresh counter.
"""

from __future__ import annotations

import math
import threading
from dataclasses import dataclass


# meiva:note
# The limiter's policy, as the gateway read it at boot.
#
# `max_failures` and `lockout_seconds` are cluster config (`gateway.login_max_failures` /
# `gateway.login_lockout_seconds`); `max_entries` is the protective memory bound (task #3696
# exception inventory): a soft cap on tracked IPs, swept on the next failure record when
# exceeded, bounding memory for a subnet scan without a background timer.
@dataclass(frozen=True)
class LoginRateLimitConfig:
    max_failures: int
    lockout_seconds: float
    max_entries: int = 10_000

    def __post_init__(self) -> None:
        if self.max_failures < 1:
            raise ValueError("max_failures must be at least 1: a lockout needs a failure")
        if self.lockout_seconds <= 0:
            raise ValueError("lockout_seconds must be positive")
        if self.max_entries < 1:
            raise ValueError("max_entries must be at least 1: a cap of 0 or less drops every entry")


class Entry:
    """Per-IP failure state."""

    __slots__ = ("failures", "last_failure_at", "locked_until")

    def __init__(self, failures: int, locked_until: float, last_failure_at: float) -> None:
        self.failures = failures
        self.locked_until = locked_until  # epoch seconds; 0 when not locked
        self.last_failure_at = last_failure_at  # epoch seconds of most recent failure


# meiva:note
# Per-IP consecutive-failure lockout, safe for concurrent requests.
#
# The gateway is single-process, so one instance covers the whole cluster;
# the application lifespan owns the instance shared by its login requests.
# The policy is a value and every call is handed the epoch second it acts at.
# Methods are idempotent under races: a lock guards the dict, and
# worst-case concurrent failures just count a couple of attempts early.
class LoginRateLimiter:
    def __init__(self, config: LoginRateLimitConfig) -> None:
        self._config = config
        self._entries: dict[str, Entry] = {}
        self._lock = threading.Lock()

    # meiva:note
    # Whole seconds until ``ip``'s lockout expires at epoch second ``now``; 0 when not locked.
    def lockout_remaining(self, ip: str, *, now: float) -> int:
        with self._lock:
            entry = self._entries.get(ip)
            if entry is None or entry.locked_until <= now:
                return 0
            return math.ceil(entry.locked_until - now)

    def record_failure(self, ip: str, *, now: float) -> None:
        """Count one failed login for ``ip``, locking it at the configured
        ``gateway.login_max_failures``.

        A stale streak (lockout expired, or no failure for a full
        ``gateway.login_lockout_seconds`` window) restarts from one — failures
        are only "consecutive" while they keep coming.
        """
        max_failures = self._config.max_failures
        lockout_seconds = self._config.lockout_seconds
        with self._lock:
            entry = self._entries.get(ip)
            if entry is None or self._is_stale(entry, now, lockout_seconds):
                entry = Entry(failures=0, locked_until=0.0, last_failure_at=now)
                self._entries[ip] = entry
            entry.failures += 1
            entry.last_failure_at = now
            if entry.failures >= max_failures:
                entry.locked_until = now + lockout_seconds
            if len(self._entries) > self._config.max_entries:
                self._sweep(now, lockout_seconds)

    def record_success(self, ip: str) -> None:
        """Clear ``ip``'s failure state — a successful login proves the IP is
        a legitimate user, at least for now."""
        with self._lock:
            self._entries.pop(ip, None)

    def reset(self) -> None:
        """Drop all state. Test isolation only — no production caller."""
        with self._lock:
            self._entries.clear()

    @staticmethod
    def _is_stale(entry: Entry, now: float, lockout_seconds: float) -> bool:
        """True when a streak no longer counts: any lockout has expired AND no
        failure happened within a full lockout window, so the run is not
        consecutive anymore."""
        return entry.locked_until <= now and now - entry.last_failure_at > lockout_seconds

    def _sweep(self, now: float, lockout_seconds: float) -> None:
        """Drop entries whose streak is stale; over the soft cap, also trim the
        oldest active entries (audit 2026-08-08 P3: stale-only sweeping left
        the dict unbounded under a many-IP attack — each new IP can stay
        non-stale for a full lockout window by failing just under the
        configured ``login_max_failures`` per window). Caller holds
        ``self._lock``."""
        stale = [
            ip for ip, entry in self._entries.items() if self._is_stale(entry, now, lockout_seconds)
        ]
        for ip in stale:
            del self._entries[ip]
        over = len(self._entries) - self._config.max_entries
        if over > 0:
            # oldest failure first — the entries least likely to be an active
            # attacker right now
            oldest = sorted(self._entries, key=lambda ip: self._entries[ip].last_failure_at)[:over]
            for ip in oldest:
                del self._entries[ip]

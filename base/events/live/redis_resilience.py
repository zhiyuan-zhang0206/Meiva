"""Weak-network resilience values shared by the sync + async redis clients.

Single source for the keepalive / connect-timeout tuning that
``base.events.live.redis_client`` and ``base.events.live.redis_listener`` both apply (audit
2026-08-08 P2: the listener used to replicate these by hand under a comment
admitting "the values are kept in sync" — the exact manual-sync drift
surface R2 exists to kill). Deliberately dependency-free (stdlib only) so
either client can import it without a cycle.

Why these values: the central Postgres/Redis sit behind a TLS-MITM corp
link; a half-dead connection must surface in tens of seconds, not the OS
default TCP timeout (minutes) that once hung exec output streaming on a
Redis hiccup. Keepalive: ~30s idle, then probe every 10s, 3 misses = dead.
`getattr` keeps the option names portable — the Linux-only TCP_KEEPIDLE
constant is absent from the macOS socket module (TCP_KEEPALIVE is the macOS
spelling).
"""

from __future__ import annotations

import socket
from dataclasses import dataclass

SOCKET_CONNECT_TIMEOUT_S = 5.0
HEALTH_CHECK_INTERVAL_S = 30
WARN_THROTTLE_S = 60.0


@dataclass(frozen=True)
class AuthRetryPolicy:
    initial_delay_s: float = 0.5
    delay_cap_s: float = 10.0
    window_s: float = 60.0
    max_attempts: int = 10
    deterministic_jitter: bool = False
    sleep_scale: float = 1.0


DEFAULT_AUTH_RETRY_POLICY = AuthRetryPolicy()


def compute_auth_retry_delay(
    *,
    failure_count: int,
    waited_s: float,
    elapsed_s: float,
    jitter_sample: float,
    policy: AuthRetryPolicy = DEFAULT_AUTH_RETRY_POLICY,
) -> float:
    remaining_s = min(
        policy.window_s - waited_s,
        policy.window_s - elapsed_s,
    )
    if remaining_s <= 0:
        return 0.0
    exponential_cap = min(
        policy.initial_delay_s * (2 ** (failure_count - 1)),
        policy.delay_cap_s,
        remaining_s,
    )
    if policy.deterministic_jitter:
        jittered = exponential_cap
    else:
        clamped = max(0.0, min(1.0, jitter_sample))
        jittered = (exponential_cap / 2.0) * (1.0 + clamped)
    return max(0.0, min(jittered, exponential_cap, remaining_s))


def should_warn_publish_failure(
    warn_last: dict[tuple[str, str], float],
    *,
    channel: str,
    error_type: str,
    now: float,
    throttle_s: float = WARN_THROTTLE_S,
) -> bool:
    key = (channel, error_type)
    last = warn_last.get(key)
    if last is None or now - last >= throttle_s:
        warn_last[key] = now
        return True
    return False


def keepalive_options() -> dict[int, int]:
    """TCP keepalive probe tuning, emitting only the options the running
    kernel defines (the option names differ across Linux / macOS)."""
    opts: dict[int, int] = {}
    idle = getattr(socket, "TCP_KEEPIDLE", None) or getattr(socket, "TCP_KEEPALIVE", None)
    if idle is not None:
        opts[idle] = 30
    intvl = getattr(socket, "TCP_KEEPINTVL", None)  # seconds between probes
    if intvl is not None:
        opts[intvl] = 10
    cnt = getattr(socket, "TCP_KEEPCNT", None)  # failed probes before giving up
    if cnt is not None:
        opts[cnt] = 3
    return opts

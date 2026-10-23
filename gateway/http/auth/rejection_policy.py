# meiva:note
# Which authentication rejections are expected noise, and when a repeat earns a warning.
#
# `gateway.http.auth.rejection_log` owns the counters and the log lines; what decides their
# severity and cadence is here, as values in and a verdict out. A rejection from a stale SSE
# reconnect or a console tab whose session died in a rollout is noise. Any other rejection
# warns once per (client, path), then stays quiet until the cooldown elapses.

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

__all__ = [
    "WARN_COOLDOWN_S",
    "RejectionVerdict",
    "idle_keys",
    "is_expected_noise",
    "rejection_verdict",
]

WARN_COOLDOWN_S = 300.0

type Key = tuple[str, str]


# meiva:note
# Whether repeated rejections here are expected, so they log at debug.
#
# Live SSE routes end in /stream or /system, except the aggregate system feed at
# /api/system/all: a stale old-bundle tab reconnects them. A rollout invalidates the browser
# session, and every open console tab retries once with a 401 the UI itself surfaces; scripted
# clients (curl / httpx / wget) do not carry `Mozilla/`.
def is_expected_noise(path: str, user_agent: str) -> bool:
    sse_poll = path.endswith(("/stream", "/system")) or path == "/api/system/all"
    return sse_poll or "Mozilla/" in user_agent


# meiva:note
# How one non-noise rejection is logged and what the key's state becomes.
@dataclass(frozen=True)
class RejectionVerdict:
    kind: Literal["first", "suppressed", "reopened"]
    # meiva:note
    # `suppressed`: the repeats counted so far; `reopened`: the repeats hidden since the last
    # warning; `first`: zero.
    repeats: int

    @property
    def warns(self) -> bool:
        return self.kind != "suppressed"


# meiva:note
# The verdict for a rejection of a key last warned at `last_warn` with `suppressed` repeats.
def rejection_verdict(*, now: float, last_warn: float | None, suppressed: int) -> RejectionVerdict:
    if last_warn is None:
        return RejectionVerdict("first", 0)
    if now - last_warn < WARN_COOLDOWN_S:
        return RejectionVerdict("suppressed", suppressed + 1)
    return RejectionVerdict("reopened", suppressed)


# meiva:note
# The keys idle for more than two warning windows, whose state is forgotten.
def idle_keys(last_warn: Mapping[Key, float], now: float) -> tuple[Key, ...]:
    stale_before = now - (2 * WARN_COOLDOWN_S)
    return tuple(key for key, warned_at in last_warn.items() if warned_at < stale_before)

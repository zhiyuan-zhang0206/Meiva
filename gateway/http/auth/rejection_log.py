"""Severity routing and warning throttling for rejected gateway authentication."""

from __future__ import annotations

import asyncio
import logging

from fastapi import Request

from base import telemetry
from gateway.http.auth.rejection_policy import (
    WARN_COOLDOWN_S,
    idle_keys,
    is_expected_noise,
    rejection_verdict,
)

_log = logging.getLogger(__name__)

# Central auth-401 observability (task #1712): the per-request log line is
# DEBUG (SSE reconnect storms, PR #665) / WARNING-throttled per (client, path),
# so the count vanished from the event-derived metrics too. This process-local
# counter is incremented on EVERY middleware rejection — including the flooded
# and throttled ones — and flushed as ONE `auth401_rejected` aggregate event
# per FLUSH_INTERVAL_S. Bounded row rate (1/min), never per rejection.
_AUTH401_FLUSH_INTERVAL_S = 60.0

# meiva:note
# Which rejections are noise and when a repeat warns: `gateway.http.auth.rejection_policy`.
# Task #1635 / PR #610 stopped new bundles from blind-retrying 401'd SSE streams;
# Task #1694 treats SSE 401s as stale old-bundle tabs: expected reconnect noise.
# Uvicorn access logs are WARNING-gated (`base/log/__init__.py` `install_stdlib_intercept`),
# so without this explicit log 401s are invisible. DEBUG keeps the forensic gateway.log
# trail out of events/Loki (only INFO+ derives). Non-stream sources stay visible at
# WARNING once per (client, path) per 300s while flood repeats are downgraded to DEBUG.


class AuthRejectionLog:
    """Authentication rejection counters and throttles owned by one gateway lifespan."""

    def __init__(self) -> None:
        self.total = 0
        self.last_warn: dict[tuple[str, str], float] = {}
        self.suppressed: dict[tuple[str, str], int] = {}

    # meiva:note
    # Log an auth rejection at the route-appropriate severity and cadence.
    #
    # Counts EVERY rejection (flooded SSE reconnects and throttled repeats
    # included) into the process-local aggregate — the log severity cadence
    # decides what a human sees, never whether the count is observable. `now` is the
    # monotonic time of the rejection.
    def log(self, request: Request, now: float) -> None:
        self.total += 1
        path = request.url.path
        client = request.client.host if request.client else "unknown"
        user_agent = request.headers.get("user-agent", "-")
        if is_expected_noise(path, user_agent):
            _log.debug(
                "auth 401: path=%s client=%s ua=%s",
                path,
                client,
                user_agent,
            )
            return

        for stale in idle_keys(self.last_warn, now):
            self.last_warn.pop(stale, None)
            self.suppressed.pop(stale, None)
        key = (client, path)
        verdict = rejection_verdict(
            now=now, last_warn=self.last_warn.get(key), suppressed=self.suppressed.get(key, 0)
        )
        if verdict.kind == "suppressed":
            self.suppressed[key] = verdict.repeats
            _log.debug(
                "auth 401: path=%s client=%s ua=%s "
                "(suppressed repeat %d until the %ds warning cooldown elapses)",
                path,
                client,
                user_agent,
                verdict.repeats,
                WARN_COOLDOWN_S,
            )
            return
        self.last_warn[key] = now
        self.suppressed.pop(key, None)
        if verdict.kind == "first":
            _log.warning("auth 401: path=%s client=%s ua=%s", path, client, user_agent)
            return
        _log.warning(
            "auth 401: path=%s client=%s ua=%s (suppressed %d repeats in the last %ds)",
            path,
            client,
            user_agent,
            verdict.repeats,
            WARN_COOLDOWN_S,
        )

    def drain(self) -> int:
        """Take the aggregate rejection count since the last flush (event-loop only).

        The middleware and the flusher both run on the gateway's single event
        loop, so no locking is needed — same convention as `gateway.http.middleware.latency`.
        """
        count = self.total
        self.total = 0
        return count


def emit_auth401_count(count: int) -> None:
    """Emit one `auth401_rejected` aggregate event for `count` rejections.

    A zero window emits nothing — keep the Prometheus series continuous but
    avoid minting zero-valued datapoints for idle gateways. Exposed separate
    from the flusher so tests can drive it directly (mirrors `latency.emit_bucket`).
    """
    if count <= 0:
        return
    telemetry.emit(
        "telemetry",
        "auth401_rejected",
        attributes={"count": count},
    )


async def auth401_flusher(rejections: AuthRejectionLog) -> None:
    """Drain the aggregate counter every `_AUTH401_FLUSH_INTERVAL_S` and emit.

    Runs as a lifespan background task. A failed flush never kills the loop —
    the next tick retries (a dropped bucket is only a monitoring gap, and the
    emit pipeline itself is already best-effort).
    """
    while True:
        await asyncio.sleep(_AUTH401_FLUSH_INTERVAL_S)
        try:
            emit_auth401_count(rejections.drain())
        except Exception:
            _log.warning("auth401 count flush failed", exc_info=True)

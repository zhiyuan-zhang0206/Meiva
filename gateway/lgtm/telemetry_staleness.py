"""Read-side heartbeat guard for the telemetry served from `telemetry_events`.

The heartbeat is the gateway's own ``gateway_latency`` telemetry event. Its
60-second flusher advances whenever request traffic exists, regardless of agent
activity, unlike ``llm_usage`` counters that legitimately idle. It also isolates
the gateway exporter: during the 2026-08-23 incident, gateway metrics stopped
while agent LLM metrics continued, so whole-event-stream freshness stayed green. The
heartbeat's newest row in `telemetry_events` says whether the gateway's own events still reach
the record.

Five minutes is five times the heartbeat cadence. It is deliberately not three
times the 15-second metric export interval: a 45-second deadline for a 60-second
heartbeat would alert during healthy operation. Missing or old samples mark
read responses stale and emit transition events; check failures themselves fail
open because each read path owns backend-outage degradation separately.

The five-minute threshold is coarse enough that checking once per minute cannot
miss it. The check cadence keeps this guard off the fleet graph's cold-path hot
loop while still detecting staleness before the next threshold-sized window.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from typing import Any

from base.log import logger
from gateway.lgtm.staleness_plan import (
    HEARTBEAT_EVENT,
    STALENESS_THRESHOLD_S,
    SourceState,
    check_due,
    staleness_step,
)

CHECK_INTERVAL_S = 60


def heartbeat_age(pool: Any, *, now: datetime) -> float | None:
    """Age in seconds of the newest heartbeat row in `telemetry_events`, if any.

    Only the last `STALENESS_THRESHOLD_S * 2` seconds are read, so the scan stays on the newest
    rows; a heartbeat older than that is as stale as a missing one.
    """
    with pool.connection() as conn:
        conn.execute("SET LOCAL statement_timeout = '3s'")
        row = conn.execute(
            "SELECT max(ts) FROM telemetry_events WHERE event_name = %s AND ts > %s",
            (
                HEARTBEAT_EVENT,
                datetime.fromtimestamp(now.timestamp() - 2 * STALENESS_THRESHOLD_S, UTC),
            ),
        ).fetchone()
    newest = row[0] if row is not None else None
    return None if newest is None else now.timestamp() - newest.timestamp()


def _emit(event_name: str, attributes: dict[str, object]) -> None:
    """Best-effort status event; the JSONL mirror survives an OTLP outage."""
    from base import telemetry

    with telemetry.failure_isolated("telemetry staleness status event"):
        telemetry.emit("telemetry", event_name, attributes=attributes)


# meiva:note
# The read-side heartbeat guard: transition state plus the check cadence.
#
# One instance per gateway process (built by the app lifespan). `check_and_report`
# is the only entry point; the rules are `gateway.lgtm.staleness_plan`.
class TelemetryStaleness:
    def __init__(self, *, check_interval_s: float = CHECK_INTERVAL_S) -> None:
        self._check_interval_s = check_interval_s
        self._states: dict[str, SourceState] = {}
        self._last_checked_s: float | None = None
        self._last_stale = False
        self._lock = threading.Lock()

    # meiva:note
    # Return whether a successful telemetry read should be marked stale.
    #
    # A heartbeat-query exception is not a staleness verdict: it is logged at debug,
    # left out of this poll's result, and does not mutate transition state. The
    # fail-open verdict is cached on the check cadence alongside successful checks.
    def check_and_report(self, pool: Any, *, now: datetime) -> bool:
        with self._lock:
            now_s = now.timestamp()
            if not check_due(self._last_checked_s, now_s, self._check_interval_s):
                return self._last_stale
            stale = False
            try:
                step = staleness_step(
                    "postgres", self._states.get("postgres"), heartbeat_age(pool, now=now), now_s
                )
            except Exception:
                logger.opt(exception=True).warning(
                    "telemetry heartbeat check failed; the verdict falls back to not-stale"
                )
            else:
                stale = step.stale
                if step.state is None:
                    self._states.pop("postgres", None)
                else:
                    self._states["postgres"] = step.state
                if step.event is not None:
                    _emit(step.event.name, step.event.attributes)
            self._last_checked_s = now_s
            self._last_stale = stale
            return stale

# meiva:note
# What the LGTM probes decide, as functions of values: whether a readiness answer certifies a
# backend, what to do with one Loki push answer (accept, retry after a pause, or fail with a
# reason), and the request and answer shapes of the write-path probe. `lgtm.py` performs the
# HTTP and the pauses.

from __future__ import annotations

import json
from dataclasses import dataclass

__all__ = [
    "Accepted",
    "Failed",
    "PushStep",
    "Retry",
    "marker_visible",
    "probe_payload",
    "push_step",
    "query_params",
    "readiness_failure",
]

NANOSECONDS_PER_SECOND = 1_000_000_000
WRITE_PROBE_LOOKBACK_SECONDS = 120


# meiva:note
# The push was accepted.
@dataclass(frozen=True)
class Accepted:
    pass


# meiva:note
# Loki throttled the push (HTTP 429) and the schedule has a pause left: wait, then push again.
@dataclass(frozen=True)
class Retry:
    sleep_s: float


# meiva:note
# The push failed for `reason`, a stable code the alert wiring keys on.
@dataclass(frozen=True)
class Failed:
    reason: str


PushStep = Accepted | Retry | Failed


# meiva:note
# What one Loki push answer means. Only a throttle (429) is retried, once per entry of
# `backoff_s`; a 5xx whose body says the ingester is shutting down is named; any other
# non-2xx is `push_http_<status>`.
def push_step(*, status: int, body: bytes, attempt: int, backoff_s: tuple[float, ...]) -> PushStep:
    if 200 <= status < 300:
        return Accepted()
    if status == 429 and attempt < len(backoff_s):
        return Retry(sleep_s=backoff_s[attempt])
    if status >= 500 and b"ingester is shutting down" in body.lower():
        return Failed("ingester_shutting_down")
    return Failed(f"push_http_{status}")


# meiva:note
# Why a backend's readiness answer does not certify it, or None when it does. Grafana's body must
# also report its database `ok`.
def readiness_failure(*, name: str, status: int, grafana_database: str | None) -> str | None:
    if not 200 <= status < 300:
        return f"{name} readiness HTTP {status}"
    if name == "grafana" and grafana_database != "ok":
        return "Grafana database is not ready"
    return None


# meiva:note
# The OTLP log the write-path probe pushes, stamped `marker_ns`, and the marker text it carries.
def probe_payload(marker_ns: int) -> tuple[str, bytes]:
    marker = f"watchdog-write-probe-{marker_ns}"
    body = json.dumps(
        {
            "resourceLogs": [
                {
                    "resource": {
                        "attributes": [
                            {"key": "agent_id", "value": {"stringValue": "watchdog"}},
                            {
                                "key": "event_name",
                                "value": {"stringValue": "watchdog-write-probe"},
                            },
                        ]
                    },
                    "scopeLogs": [
                        {
                            "logRecords": [
                                {
                                    "timeUnixNano": str(marker_ns),
                                    "body": {"stringValue": marker},
                                }
                            ]
                        }
                    ],
                }
            ]
        }
    ).encode()
    return marker, body


# meiva:note
# The query that reads the marker back. The range end is exclusive, so `query_end_ns` is a fresh
# reading taken after the push; the window is anchored on the marker.
def query_params(*, marker: str, marker_ns: int, query_end_ns: int) -> dict[str, str]:
    return {
        "query": f'{{agent_id="watchdog", event_name="watchdog-write-probe"}} |= "{marker}"',
        "start": str(marker_ns - (WRITE_PROBE_LOOKBACK_SECONDS * NANOSECONDS_PER_SECOND)),
        "end": str(query_end_ns),
    }


# meiva:note
# Whether a Loki `query_range` answer holds the marker line.
def marker_visible(
    payload: dict[str, dict[str, list[dict[str, list[list[str]]]]]], marker: str
) -> bool:
    return any(
        value[1] == marker for stream in payload["data"]["result"] for value in stream["values"]
    )

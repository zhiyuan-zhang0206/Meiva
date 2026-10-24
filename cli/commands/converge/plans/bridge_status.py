# meiva:note
# What the Redis bridge probe reports, and how a report reads.
#
# The probe shell finds the relay's endpoint and pings Redis through it; this module assembles
# the status from those findings and words the one line `meiva status` prints for it.

from __future__ import annotations

from typing import NamedTuple

__all__ = [
    "RedisBridgeStatus",
    "failure_detail",
    "not_required",
    "ping_status",
    "status_line",
    "unconfigured",
]


# meiva:note
# `required` is whether this host needs the relay at all; `serving` whether a PING through it
# worked (True when it is not required); `detail` says why in one line.
class RedisBridgeStatus(NamedTuple):
    required: bool
    endpoint: str
    serving: bool
    detail: str


# meiva:note
# This host needs no relay.
def not_required() -> RedisBridgeStatus:
    return RedisBridgeStatus(required=False, endpoint="", serving=True, detail="not required")


# meiva:note
# The relay is needed but the home cannot name its port.
def unconfigured(reason: str) -> RedisBridgeStatus:
    return RedisBridgeStatus(required=True, endpoint="", serving=False, detail=reason)


# meiva:note
# The last non-empty line of a client error, or the exception's type when it has no text.
def failure_detail(message: str, kind: str) -> str:
    lines = [line.strip() for line in message.splitlines() if line.strip()]
    return lines[-1] if lines else kind


# meiva:note
# The status after a PING through `endpoint`; `failure` is None when the PING succeeded.
def ping_status(endpoint: str, failure: str | None) -> RedisBridgeStatus:
    if failure is not None:
        return RedisBridgeStatus(required=True, endpoint=endpoint, serving=False, detail=failure)
    return RedisBridgeStatus(
        required=True, endpoint=endpoint, serving=True, detail="Redis PING succeeded"
    )


# meiva:note
# The line `meiva status` prints under the relay heading.
def status_line(status: RedisBridgeStatus) -> str:
    if not status.required:
        return "  · not required (Redis has no local off-box ingress)"
    mark = "✓" if status.serving else "✗"
    endpoint = f" {status.endpoint}" if status.endpoint else ""
    return f"  {mark}{endpoint} Redis PING: {status.detail}"

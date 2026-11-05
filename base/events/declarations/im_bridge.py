"""IM bridge delivery-health events."""

from __future__ import annotations

from typing import TypedDict

from base.events.vocabulary import EventSpec, telemetry_event


class ImPushFailed(TypedDict):
    """`im_push_failed` payload — services/entrypoints/im_bridge/push_watchdog.py.

    One event per outbound send whose single retry also failed. `failures` is
    the adapter's consecutive-failure count (weixin: the iLink context_token
    expired; only the user's own message refreshes it)."""

    channel: str
    failures: int


class ImPushHealth(TypedDict):
    """`im_push_health` payload -- services/entrypoints/im_bridge/push_health.py.

    One sample per configured channel per sampling interval: `last_ok_age_s`
    is the seconds since the channel's newest delivered push (the negative
    PUSH_HEALTH_NEVER_SENT sentinel when none is on record); `pending` is the
    count of accepted pushes still waiting for a delivery attempt. The Grafana
    rule owns the staleness threshold and the notification (task #4935)."""

    channel: str
    last_ok_age_s: float
    pending: int


EVENTS: dict[str, EventSpec] = {
    "im_push_failed": telemetry_event(
        "im_push_failed",
        "an IM bridge outbound send failed after its single retry (failures = the "
        "adapter's consecutive-failure count)",
        payload=ImPushFailed,
        tier="anomaly",
    ),
    "im_push_health": telemetry_event(
        "im_push_health",
        "one per-channel IM bridge push-health sample: age of the last delivered "
        "push and pending deliveries, consumed by the leg-staleness alert rule",
        payload=ImPushHealth,
    ),
}

"""Inbound message delivery watchdog events."""

from __future__ import annotations

from typing import TypedDict

from base.events.vocabulary import EventSpec, telemetry_event


class DeliveryStalled(TypedDict):
    """`delivery_stalled` payload — services/wake/delivery_watchdog/daemon.py."""

    inbound_id: int
    age_s: float


# meiva:note
# `delivery_stranded_wake` payload — emitted by
# `services/wake/delivery_watchdog/stranded_wake.py`.
class DeliveryStrandedWake(TypedDict):
    inbound_id: int
    kind: str


class InboundConsumedUnknown(TypedDict):
    inbound_id: int
    kind: str
    age_s: float
    content_preview: str


EVENTS: dict[str, EventSpec] = {
    "inbound_consumed_unknown": telemetry_event(
        "inbound_consumed_unknown",
        "a claimed inbound was consumed without knowing whether its work ran "
        "(host crash / aborted turn); the row is marked in its payload and is "
        "never redelivered — this event is the only record of what was lost",
        payload=InboundConsumedUnknown,
        tier="anomaly",
        site="agent/db/__init__.py:finalize_claimed_inbounds",
        persist=True,
    ),
    "delivery_stranded_wake": telemetry_event(
        "delivery_stranded_wake",
        "a message reached a terminated owner with no live consumer and the "
        "delivery fallback woke it: the delivery-time auto-resurrect did not run "
        "because the message committed while the owner still read running",
        payload=DeliveryStrandedWake,
        tier="anomaly",
        site="services/wake/delivery_watchdog/stranded_wake.py:_emit_stranded_wake",
        persist=True,
    ),
    "delivery_stalled": telemetry_event(
        "delivery_stalled",
        "delivery backlog",
        payload=DeliveryStalled,
        tier="anomaly",
        site="services/wake/delivery_watchdog/daemon.py:_alert_stalled",
        persist=True,
    ),
}

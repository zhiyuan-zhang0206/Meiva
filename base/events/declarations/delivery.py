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


# meiva:note
# A turn that faulted before it produced any text: the inbounds that started it are consumed by the
# queue's at-most-once rule and can never become a reply. `reason` is the harness's own sentence
# (the registered `turn_fault_lost_inbound` event is what makes that loss countable, beside the
# conversation note the reader sees).
class TurnFaultLostInbound(TypedDict):
    harness: str
    turn_key: str
    inbound_ids: list[int]
    reason: str


# meiva:note
# The note that should have recorded a lost turn could not be written. The turn's own failure still
# propagates (that is the caller's contract), so this event is the only trace that the conversation
# has no record of the inbound that died with it.
class TurnFaultNoteFailed(TypedDict):
    agent_id: int
    turn_key: str


class InboundConsumedUnknown(TypedDict):
    inbound_id: int
    kind: str
    age_s: float
    content_preview: str


EVENTS: dict[str, EventSpec] = {
    "turn_fault_lost_inbound": telemetry_event(
        "turn_fault_lost_inbound",
        "a harness turn faulted before producing any text; the inbounds that started it were "
        "consumed and are never redelivered",
        payload=TurnFaultLostInbound,
        tier="anomaly",
        site="agent/graph/claim/harness_turn.py:_report_lost_turn",
    ),
    "turn_fault_note_failed": telemetry_event(
        "turn_fault_note_failed",
        "a lost turn could not be recorded in the conversation: the note append raised, the "
        "original fault propagates anyway, and the inbound that died leaves no trace the reader "
        "can see",
        payload=TurnFaultNoteFailed,
        tier="anomaly",
        site="agent/graph/claim/lost_turn.py:report_lost_turn",
    ),
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

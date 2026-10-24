# meiva:note
# Builders for the relay's decision inputs: inbox rows, snapshots and clock readings. Every
# time is a literal, so a decision under test is a function of what the test wrote down.

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from base.agents.impersonation.status import ImpersonationStatus
from cli.commands.agents.relay.inbox import (
    DeliveryBudget,
    EndedLease,
    InboxMessage,
    InboxSnapshot,
    LeaseRef,
    routine_ids,
)
from cli.commands.agents.relay.steps import ClockReading, RelayTuning

__all__ = [
    "LEASE_UUID",
    "NOW",
    "REF",
    "START_MESSAGE",
    "TUNING",
    "clock",
    "message",
    "snapshot",
]

LEASE_UUID = UUID("767fb040-aa54-42ae-b2c8-594039fbbf46")
NOW = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
REF = LeaseRef(agent_id=42, lease_ref=5)
START_MESSAGE = "Start here: resume the implementation from the failing test."
TUNING = RelayTuning(debounce=0.0, max_chars=None, min_emit_interval=0.0)


def message(
    message_id: int,
    *,
    kind: str = "system_note",
    source: str = "system",
    content: str | None = None,
    attempts: int = 0,
    due: bool = True,
) -> InboxMessage:
    return InboxMessage(
        id=message_id,
        kind=kind,
        source=source,
        content=content or f"body {message_id}",
        delivery_attempts=attempts,
        delivery_due=due,
    )


def snapshot(
    *messages: InboxMessage,
    status: ImpersonationStatus = ImpersonationStatus.ACTIVE,
    lifetime_s: float = 300.0,
    batch_window: float = 0.0,
    start_message: str = START_MESSAGE,
    ack_window_s: int = 180,
    max_attempts: int = 2,
    ended: EndedLease | None = None,
    end_reason: str | None = None,
) -> InboxSnapshot:
    return InboxSnapshot(
        messages=messages,
        expires_at=NOW + timedelta(seconds=lifetime_s),
        status=status,
        routine_ids=routine_ids(messages),
        batch_window=batch_window,
        start_message=start_message,
        budget=DeliveryBudget(ack_window_s, max_attempts),
        end_reason=end_reason,
        ended=ended,
    )


def clock(monotonic: float = 0.0) -> ClockReading:
    return ClockReading(monotonic_now=monotonic, wall_now=NOW + timedelta(seconds=monotonic))

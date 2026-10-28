"""Impersonation lease status vocabulary and status-only row validation."""

from collections.abc import Mapping
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal, TypedDict, TypeGuard
from uuid import UUID

from pydantic import BaseModel


# meiva:note
# Declared impersonation identity attached to rendered messages without copying session-wide process metadata.
class ImpersonationMetadata(BaseModel):
    agent_id: int
    session_id: int
    name: str
    executor_name: str
    provider: str | None
    anchor_item_id: str | None = None
    seq: int | None = None


class ImpersonationStatus(StrEnum):
    """Persisted cooperative lease lifecycle; distinct from native agent status."""

    REQUESTED = "requested"
    ACCEPTED = "accepted"
    ACTIVE = "active"
    RELEASED = "released"
    REJECTED = "rejected"
    EXPIRED = "expired"


OpenImpersonationStatus = Literal[
    ImpersonationStatus.REQUESTED, ImpersonationStatus.ACCEPTED, ImpersonationStatus.ACTIVE
]
OPEN: tuple[OpenImpersonationStatus, ...] = (
    ImpersonationStatus.REQUESTED,
    ImpersonationStatus.ACCEPTED,
    ImpersonationStatus.ACTIVE,
)


class LeaseRecord(TypedDict):
    """Lifecycle row: status is parsed; other database fields retain their existing types."""

    status: ImpersonationStatus
    session_id: int
    next_entry: int
    handoff_path: str | None
    events_completed_at: datetime | None
    event_delivery_protocol_version: int | None
    event_delivery_pending_reason: str | None
    event_admission_closed_at: datetime | None
    ended_at: datetime | None
    accepted_generation: UUID | None
    accepted_owner: UUID | None
    ack_window_seconds: int
    agent_id: int
    applied_version: int
    delta_version: int
    expires_at: datetime
    handoff_applied_at: datetime | None
    id: UUID | str
    machine: str
    max_delivery_attempts: int
    relay_provider: str | None
    relay_generation: int
    relay_heartbeat_at: datetime | None
    relay_minted_at: datetime | None
    relay_minted_generation: UUID | None
    relay_minted_owner: UUID | None
    relay_degraded_reason: str | None
    relay_token_hash: str | None
    process_metadata: dict[str, Any]
    source: str
    summary_inbound_id: int | None
    summary: str | None
    rejection_reason: str | None
    ttl_seconds: int


def _is_lease_record(row: dict[str, object]) -> TypeGuard[LeaseRecord]:
    return isinstance(row["status"], ImpersonationStatus)


def parse_lease(row: dict[str, object]) -> LeaseRecord:
    """Validate only status as a raw SQL row enters lease lifecycle logic."""
    # meiva:note
    # Invalid status strings or non-strings from untrusted DB rows must raise ValueError
    # consistently across enum parsing and pre-enum guard checks.
    raw_status = row["status"]
    if not isinstance(raw_status, str):
        raise ValueError(f"{raw_status!r} is not a valid ImpersonationStatus")  # noqa: TRY004
    row["status"] = ImpersonationStatus(raw_status)
    if _is_lease_record(row):
        return row
    raise TypeError("Lease status must be an ImpersonationStatus")


# Wire version of the event log a lease records into its own entries (protocol v2).
LOG_PROTOCOL_VERSION = 2

# Relay heartbeat cadence, the staleness window after which a wake alerts, and how long a recorded
# failure suppresses a repeat alert.
RELAY_HEARTBEAT_SECONDS = 10.0
RELAY_HEARTBEAT_STALE_SECONDS = 45.0
RELAY_FAILURE_STAMP_SECONDS = 600

# meiva:note
# The waits one relay spends on a database connection the pool could not provide, in order; the
# sum is the whole retry budget. It stays under RELAY_HEARTBEAT_STALE_SECONDS because a relay
# that cannot reach the database cannot beat either: past that window every other reader
# already treats the lease as stale and the host ends it, so further retrying only guards a
# lease that is gone. It stays above one heartbeat interval so a single pooled-connection blip
# never ends a relay.
RELAY_CONNECTION_RETRY_DELAYS = (1.0, 2.0, 4.0, 8.0, 12.0)


def is_log_native(lease: Mapping[str, object]) -> bool:
    """Whether this lease records its events in its own entries (protocol v2)."""
    return lease["event_delivery_protocol_version"] == LOG_PROTOCOL_VERSION


def pending_reason(lease: Mapping[str, object]) -> str | None:
    """Classify a handoff's current delivery uncertainty without inventing state."""
    if lease["events_completed_at"] is not None:
        return None
    if lease["event_delivery_protocol_version"] != LOG_PROTOCOL_VERSION:
        return "legacy"
    stored = lease["event_delivery_pending_reason"]
    if stored is not None:
        return str(stored)
    return "awaiting_session_end" if lease["ended_at"] is None else "awaiting_participant_seal"

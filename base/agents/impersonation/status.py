"""Impersonation lease status vocabulary and status-only row validation."""

from collections.abc import Mapping
from datetime import datetime
from enum import StrEnum
from typing import Literal, TypedDict, TypeGuard
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


# meiva:note
# The lease columns that read the same in the stored row and in the public projection: nothing
# secret and no UUID. `LeaseRecord` and `PublicLease` add what differs.
class LeaseColumns(TypedDict):
    status: ImpersonationStatus
    session_id: int
    name: str
    executor_name: str
    reason: str
    start_message: str
    plugin_delta: list[object]
    created_at: datetime
    activated_at: datetime | None
    next_entry: int
    handoff_path: str | None
    handoff_document: object
    events_completed_at: datetime | None
    event_delivery_protocol_version: int | None
    event_delivery_pending_reason: str | None
    event_admission_closed_at: datetime | None
    ended_at: datetime | None
    ack_window_seconds: int
    agent_id: int
    applied_version: int
    delta_version: int
    expires_at: datetime
    handoff_applied_at: datetime | None
    machine: str
    max_delivery_attempts: int
    relay_provider: str | None
    relay_thread_id: str | None
    relay_codex_remote: str | None
    relay_batch_window_seconds: int
    relay_generation: int
    relay_heartbeat_at: datetime | None
    relay_last_failure_at: datetime | None
    relay_minted_at: datetime | None
    relay_degraded_reason: str | None
    process_metadata: dict[str, object]
    source: str
    summary_inbound_id: int | None
    summary: str | None
    rejection_reason: str | None
    ttl_seconds: int


class LeaseRecord(LeaseColumns):
    """Lifecycle row: status is parsed; other database fields retain their existing types."""

    id: UUID | str
    accepted_generation: UUID | None
    accepted_owner: UUID | None
    relay_minted_generation: UUID | None
    relay_minted_owner: UUID | None
    relay_token_hash: str | None


# meiva:note
# What `public` hands out: the lease row without its credentials, its UUIDs rendered as strings.
class PublicLease(LeaseColumns):
    id: str
    accepted_generation: str | None
    accepted_owner: str | None
    relay_minted_generation: str | None
    relay_minted_owner: str | None


# meiva:note
# The relay provider of a lease that spawns a relay. The lease row's relay spec constraint
# guarantees it for codex and antigravity, so a lease without one reaching a spawn is a broken
# invariant, refused by name.
def relay_provider(lease: PublicLease) -> str:
    provider = lease["relay_provider"]
    if provider is None:
        raise ValueError(f"lease {lease['id']} carries no relay provider")
    return provider


# meiva:note
# The relay thread of a lease that spawns a relay; see `relay_provider`.
def relay_thread_id(lease: PublicLease) -> str:
    thread_id = lease["relay_thread_id"]
    if thread_id is None:
        raise ValueError(f"lease {lease['id']} carries no relay thread id")
    return thread_id


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

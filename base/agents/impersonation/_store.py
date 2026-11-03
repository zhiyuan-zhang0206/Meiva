"""Short, agent-row-serialized transactions for cooperative impersonation."""

import hashlib
import hmac
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import TypedDict, TypeGuard
from uuid import UUID

from psycopg.rows import class_row, dict_row

from base.agents.impersonation.status import (
    OPEN,
    ImpersonationStatus,
    LeaseRecord,
    PublicLease,
    is_log_native,
    parse_lease,
)
from base.api_contracts.agent_contract import AgentStatus, Harness
from base.db.protocols import SyncConnection
from base.db.rows import IdRow
from base.native_process.runtime_incarnation import RuntimeIncarnation


class ImpersonationError(RuntimeError):
    """The lease, local placement, or expected ownership does not permit work."""


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# meiva:note
# One pending inbox message as a controller reads it. The payload is the stored envelope, still to
# be checked by whoever reads into it.
class InboxMessage(TypedDict):
    id: int
    content: str
    kind: str
    source: str
    payload: object
    created_at: datetime


# meiva:note
# An inbox message as the relay reads it, with its delivery state on the lease.
class RelayInboxMessage(InboxMessage):
    delivery_attempts: int
    delivery_due: bool


# meiva:note
# The answer of a statement that selects the `id` of a lease (a UUID).
@dataclass(frozen=True)
class LeaseIdRow:
    id: UUID


# meiva:note
# The agent columns impersonation reads while it holds the agent row lock.
@dataclass(frozen=True)
class LockedAgent:
    id: int
    status: str
    machine: str
    harness: str
    runtime_generation: UUID | None
    runtime_owner: UUID | None


def lock_agent(conn: SyncConnection, agent_id: int) -> LockedAgent:
    with conn.cursor(row_factory=class_row(LockedAgent)) as cur:
        row = cur.execute(
            "SELECT id,status,machine,harness,runtime_generation,runtime_owner "
            "FROM agents_meta WHERE id=%s FOR UPDATE",
            (agent_id,),
        ).fetchone()
    if row is None:
        raise ImpersonationError(f"Agent {agent_id} does not exist")
    return row


def lock_lease(conn: SyncConnection, lease_id: str) -> LeaseRecord:
    # Read only the immutable foreign key first; all mutations acquire the agent
    # lock before the lease or inbox, matching native ownership and claim order.
    with conn.cursor(row_factory=class_row(IdRow)) as id_cur:
        row = id_cur.execute(
            "SELECT agent_id AS id FROM agent_impersonations WHERE id=%s",
            (UUID(str(lease_id)),),
        ).fetchone()
    if row is None:
        raise ImpersonationError("Impersonation does not exist")
    lock_agent(conn, row.id)
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT * FROM agent_impersonations WHERE id=%s FOR UPDATE", (lease_id,))
        lease = cur.fetchone()
    if lease is None:
        raise ImpersonationError("Impersonation disappeared")
    return parse_lease(lease)


# meiva:note
# The shape `public` promises beyond the status it checks is the lease row's own: the rows are
# read whole from `agent_impersonations`, as `parse_lease` does for the stored record.
def _is_public_lease(row: dict[str, object]) -> TypeGuard[PublicLease]:
    return isinstance(row.get("status"), ImpersonationStatus)


def public(lease: Mapping[str, object]) -> PublicLease:
    status = ImpersonationStatus(lease["status"])
    projected: dict[str, object] = {
        key: status if key == "status" else str(value) if isinstance(value, UUID) else value
        for key, value in lease.items()
        if key not in ("token_hash", "relay_token_hash", "automatic", "consent_version")
    }
    if not _is_public_lease(projected):
        raise TypeError("A public lease needs a parsed status")
    return projected


# meiva:note
# The columns `live_lease_on_machine` reads.
@dataclass(frozen=True)
class _LiveLeaseRow:
    id: str
    source: str
    executor_name: str


# meiva:note
# Which takeover, if any, a restart on `machine` would strand: the newest open lease and a
# readable holder, never the token hash. Status is the authority and the clock only guards a
# lagging reaper -- a released lease keeps its future `expires_at` (real rows show 4h/8h TTLs
# ended early), so testing the clock alone reports a lease nobody holds. Read-only: the
# caller owns the transaction and the failure semantics.
def live_lease_on_machine(conn: SyncConnection, machine: str) -> tuple[str, str] | None:
    with conn.cursor(row_factory=class_row(_LiveLeaseRow)) as cur:
        row = cur.execute(
            "SELECT id::text AS id, source, executor_name FROM agent_impersonations "
            "WHERE machine=%s AND status IN ('requested','accepted','active') "
            "AND expires_at>clock_timestamp() "
            "ORDER BY expires_at DESC, agent_id LIMIT 1",
            (machine,),
        ).fetchone()
    if row is None:
        return None
    return row.id, f"{row.source} / {row.executor_name}" if row.executor_name else row.source


# meiva:note
# The one wording for a same-machine refusal, naming the agent and both machines:
# the operator's next question is always "which host is the agent on, and which
# one am I on", and the bare sentence turned every mismatch into a manual hunt.
# Callers keep their own exception types — request/local raise ImpersonationError,
# history.resolve raises ValueError — so only the text is shared.
def machine_mismatch_message(*, agent_id: int, agent_machine: str, local_machine: str) -> str:
    return (
        "Impersonation is limited to the agent's own machine:"
        f" agent {agent_id} is on {agent_machine!r}, this process is on {local_machine!r}"
    )


def local(
    lease: LeaseRecord,
    *,
    local_machine: str,
) -> None:
    if lease["machine"] != local_machine:
        raise ImpersonationError(
            machine_mismatch_message(
                agent_id=lease["agent_id"],
                agent_machine=lease["machine"],
                local_machine=local_machine,
            )
        )


def authenticate(
    lease: LeaseRecord,
    *,
    local_machine: str,
) -> None:
    """Controller authority requires same-machine placement."""
    local(
        lease,
        local_machine=local_machine,
    )


def require_meiva_harness(agent_id: int, harness: str) -> None:
    parsed = Harness(harness)
    if parsed is not Harness.MEIVA:
        raise ImpersonationError(
            f"Impersonation requires the Meiva harness: agent {agent_id} uses {parsed.value!r}"
        )


def require_native(conn: SyncConnection, incarnation: RuntimeIncarnation) -> LockedAgent:
    meta = lock_agent(conn, incarnation.agent_id)
    require_meiva_harness(meta.id, meta.harness)
    if (meta.runtime_generation, meta.runtime_owner) != (
        incarnation.generation,
        incarnation.owner,
    ) or AgentStatus(meta.status) not in (AgentStatus.RUNNING, AgentStatus.IDLING):
        raise ImpersonationError("Native runtime no longer owns this agent")
    return meta


def dismiss_reminders(conn: SyncConnection, lease: LeaseRecord) -> None:
    """Retire this lease's pending renewal reminders, in the lease transaction.

    Reminders are written for the external controller only; once the lease
    ends (release or expiry) they are meaningless to the native inbox. ACKed
    rows are already done. The payload key matches the maintenance insert.
    """
    conn.execute(
        "UPDATE inbound_messages SET status='done' "
        "WHERE agent_id=%s AND kind='reminder' AND status='pending' "
        "AND payload->>'lease_id'=%s",
        (lease["agent_id"], str(lease["id"])),
    )


def expire(conn: SyncConnection, lease: LeaseRecord) -> LeaseRecord:
    if lease["status"] not in OPEN:
        return lease
    fresh = conn.execute("SELECT %s > clock_timestamp()", (lease["expires_at"],)).fetchone()
    # Reconcile across the entire lease, not just the relay's bounded inbox
    # page. ACK and delivery reservations take this same agent/lease lock.
    overdue = (
        conn.execute(
            "SELECT m.inbound_id FROM agent_impersonation_messages m "
            "JOIN inbound_messages i ON i.id=m.inbound_id "
            "WHERE m.lease_id=%s AND m.acknowledged_at IS NULL AND i.status='pending' "
            "AND m.delivery_attempts >= %s "
            "AND m.last_delivery_at <= clock_timestamp() - %s*interval '1 second' "
            "ORDER BY m.inbound_id LIMIT 1",
            (lease["id"], lease["max_delivery_attempts"], lease["ack_window_seconds"]),
        ).fetchone()
        if lease["status"] == ImpersonationStatus.ACTIVE
        else None
    )
    if fresh == (True,):
        if overdue is not None:
            reason = f"message {overdue[0]} exhausted its delivery budget; receipt remains possible"
            conn.execute(
                "UPDATE agent_impersonations SET relay_degraded_reason=%s,"
                "relay_degraded_at=clock_timestamp() WHERE id=%s AND relay_degraded_reason IS DISTINCT FROM %s",
                (reason, lease["id"], reason),
            )
            lease["relay_degraded_reason"] = reason
        return lease
    if is_log_native(lease):
        close_event_admission(conn, str(lease["id"]))
    conn.execute(
        "UPDATE agent_impersonations SET status='expired',ended_at=clock_timestamp() WHERE id=%s",
        (lease["id"],),
    )
    # A pending renewal reminder only matters to the external session; the
    # lease is over, so it must never reach the native agent's inbox.
    dismiss_reminders(conn, lease)
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT * FROM agent_impersonations WHERE id=%s", (lease["id"],))
        refreshed = cur.fetchone()
        assert refreshed is not None  # noqa: S101 - locked session exists
        lease = parse_lease(refreshed)
    lease["status"] = ImpersonationStatus.EXPIRED
    return lease


def validate_active(
    lease: LeaseRecord,
    *,
    fresh: bool,
    machine: str,
    status: AgentStatus,
    local_machine: str,
) -> None:
    """Validate either a joined read snapshot or rows already locked for mutation."""
    if lease["status"] != ImpersonationStatus.ACTIVE or not fresh:
        raise ImpersonationError(
            "Impersonation session is not active (stale-session): it ended or its "
            "TTL expired; the native agent continues."
        )
    if machine != lease["machine"] or status not in (AgentStatus.RUNNING, AgentStatus.IDLING):
        raise ImpersonationError("Agent placement or lifecycle changed")
    authenticate(
        lease,
        local_machine=local_machine,
    )


def require_active_locked(
    conn: SyncConnection,
    lease: LeaseRecord,
    *,
    local_machine: str,
) -> None:
    # Do not persist expiration here and then raise (which would roll it back).
    # The native boundary/get reconciler persists it independently.
    meta = lock_agent(conn, lease["agent_id"])
    require_meiva_harness(meta.id, meta.harness)
    fresh = conn.execute("SELECT %s > clock_timestamp()", (lease["expires_at"],)).fetchone()
    validate_active(
        lease,
        fresh=fresh == (True,),
        machine=meta.machine,
        status=AgentStatus(meta.status),
        local_machine=local_machine,
    )


RELAY_PROVIDERS = ("codex", "claude", "dsh", "antigravity")
# Relays that run inside the controller's own session: the request mints their
# scoped credential, the activation gate waits for their heartbeat, and the
# native side never spawns or re-provisions them. codex and antigravity relays
# are spawned by the accepting runtime instead.
SESSION_RELAY_PROVIDERS = ("claude", "dsh")


def validate_relay_spec(
    provider: str | None, thread_id: str | None, codex_remote: str | None
) -> None:
    """The request must name its relay endpoint up front; the native side
    never guesses one. A codex remote must be a unix:// or ws:// address,
    rejected here so a malformed endpoint fails the request instead of the
    relay (review N4)."""
    if provider not in RELAY_PROVIDERS:
        raise ValueError("Relay provider must be 'codex', 'claude', 'dsh' or 'antigravity'")
    if provider == "codex":
        if not thread_id:
            raise ValueError("Codex relay requires the existing session's thread id")
        if codex_remote is not None and not codex_remote.startswith(("unix://", "ws://")):
            raise ValueError("Codex remote must be a unix:// or ws:// endpoint")
    elif provider == "antigravity":
        if not thread_id or not thread_id.strip():
            raise ValueError("Antigravity relay requires the conversation's thread id")
        if codex_remote is not None:
            raise ValueError("The antigravity relay does not use --codex-remote")
    elif thread_id is not None or codex_remote is not None:
        raise ValueError(
            f"The {provider} relay routes to its owner; thread id and remote are rejected"
        )


def authenticate_relay(
    lease: LeaseRecord,
    relay_token: str,
    *,
    local_machine: str,
) -> None:
    """The relay's scoped credential: inbox/delivery/beat, never controller authority."""
    if lease["relay_token_hash"] is None or not hmac.compare_digest(
        lease["relay_token_hash"], token_hash(relay_token)
    ):
        raise ImpersonationError("Invalid relay token")
    local(
        lease,
        local_machine=local_machine,
    )


def require_relay_active_locked(
    conn: SyncConnection,
    lease: LeaseRecord,
    relay_token: str,
    *,
    local_machine: str,
) -> None:
    meta = lock_agent(conn, lease["agent_id"])
    require_meiva_harness(meta.id, meta.harness)
    fresh = conn.execute("SELECT %s > clock_timestamp()", (lease["expires_at"],)).fetchone()
    authenticate_relay(
        lease,
        relay_token,
        local_machine=local_machine,
    )
    if lease["status"] != ImpersonationStatus.ACTIVE or fresh != (True,):
        raise ImpersonationError("Impersonation is not active or its TTL has expired")
    if meta.machine != lease["machine"] or AgentStatus(meta.status) not in (
        AgentStatus.RUNNING,
        AgentStatus.IDLING,
    ):
        raise ImpersonationError("Agent placement or lifecycle changed")


def close_event_admission(conn: SyncConnection, lease_id: str) -> bool:
    """Close a log-native lease's admission gate through its narrow SQL door."""
    row = conn.execute(
        "SELECT close_impersonation_event_admission(%s)",
        (lease_id,),
    ).fetchone()
    return row is not None and row[0] is True

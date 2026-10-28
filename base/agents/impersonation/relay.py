"""Relay binding and failure paths for cooperative impersonation leases."""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from base.agents.impersonation._store import (
    OPEN,
    ImpersonationError,
    authenticate_relay,
    dismiss_reminders,
    expire,
    lock_lease,
    public,
    require_native,
    require_relay_active_locked,
    token_hash,
)
from base.agents.impersonation.status import (
    RELAY_FAILURE_STAMP_SECONDS,
    RELAY_HEARTBEAT_STALE_SECONDS,
    ImpersonationStatus,
    parse_lease,
)
from base.agents.impersonation.wake import wake_agent
from base.api_contracts.caller_identity import caller_payload
from base.db.protocols import DatabaseLike, SyncConnection
from base.events.live.bus import EventBus
from base.log import logger
from base.native_process.runtime_incarnation import RuntimeIncarnation


def relay_get(
    db: DatabaseLike,
    bus: EventBus,
    lease_id: str,
    relay_token: str,
    *,
    local_machine: str,
) -> dict[str, Any]:
    """Lease reads for the bound relay process, under its scoped credential."""

    with db.write_transaction() as conn:
        lease = lock_lease(conn, lease_id)
        authenticate_relay(
            lease,
            relay_token,
            local_machine=local_machine,
        )
        was_open = lease["status"] in OPEN
        result = public(expire(conn, lease))
    if was_open and result["status"] == ImpersonationStatus.EXPIRED:
        wake_agent(db, bus, lease["agent_id"], roster_changed=True)
    return result


def relay_inbox(
    db: DatabaseLike,
    lease_id: str,
    relay_token: str,
    *,
    limit: int = 100,
    local_machine: str,
) -> list[dict[str, Any]]:
    """Read the relay's bounded inbox page and durable attempt state."""
    if not 1 <= limit <= 1000:
        raise ValueError("Inbox limit must be from 1 through 1000")
    with db.write_transaction() as conn:
        lease = lock_lease(conn, lease_id)
        require_relay_active_locked(
            conn,
            lease,
            relay_token,
            local_machine=local_machine,
        )
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT i.id,i.content,i.kind,i.source,i.payload,i.created_at,"
                "COALESCE(m.delivery_attempts,0) AS delivery_attempts,"
                "(m.last_delivery_at IS NULL OR m.last_delivery_at <= "
                "clock_timestamp() - %s*interval '1 second') AS delivery_due "
                "FROM inbound_messages i LEFT JOIN agent_impersonation_messages m "
                "ON m.inbound_id=i.id AND m.lease_id=%s "
                "WHERE i.agent_id=%s AND i.status='pending' AND i.kind IN "
                "('chat','system_note','cancel','reminder','heartbeat') "
                "ORDER BY i.id LIMIT %s",
                (lease["ack_window_seconds"], lease_id, lease["agent_id"], limit),
            )
            messages = cur.fetchall()
        for message in messages:
            conn.execute(
                "INSERT INTO agent_impersonation_messages(lease_id,inbound_id) VALUES(%s,%s) "
                "ON CONFLICT DO NOTHING",
                (lease_id, message["id"]),
            )
    return messages


def relay_heartbeat(
    db: DatabaseLike,
    lease_id: str,
    relay_token: str,
    *,
    local_machine: str,
) -> None:
    """Fresh liveness evidence from the bound relay, while the lease is open."""
    with db.write_transaction() as conn:
        lease = lock_lease(conn, lease_id)
        authenticate_relay(
            lease,
            relay_token,
            local_machine=local_machine,
        )
        if lease["status"] not in OPEN:
            raise ImpersonationError("Impersonation has ended")
        conn.execute(
            "UPDATE agent_impersonations SET relay_heartbeat_at=clock_timestamp() WHERE id=%s",
            (lease_id,),
        )


class RelayAction(StrEnum):
    # meiva:note
    # What this incarnation did with the lease's spawned relay credential.

    MINTED = "minted"
    ADOPTED = "adopted"


@dataclass(frozen=True)
class RelayProvision:
    # meiva:note
    # One provisioning decision and the lease it left behind. `MINTED` names a credential
    # this call created: the caller spawns the relay. `ADOPTED` names the credential an
    # earlier incarnation created and whose carrier is still beating: the caller spawns
    # nothing and waits for that carrier instead.

    action: RelayAction
    lease: Mapping[str, Any]


def provision_relay(
    db: DatabaseLike,
    lease_id: str,
    incarnation: RuntimeIncarnation,
    relay_token: str,
) -> RelayProvision | None:
    # meiva:note
    # Mint the lease's spawned-relay credential, or adopt the one already beating.
    #
    # A hosted restart admits a different incarnation while the relay it spawned keeps
    # running (`start_new_session`), so the credential outlives its minter. This incarnation
    # adopts it while its heartbeat is fresh and re-mints only when it is not: a carrier
    # that stopped beating is not adopted silently, and re-minting invalidates the old token
    # so the stale carrier exits instead of delivering beside the new one.
    #
    # None means this call must not write: this incarnation already minted for this lease
    # (two activation passes raced; the loser performs no writes), or the lease is not in a
    # mintable state. The decision keys on `relay_minted_generation`, never on
    # `relay_generation`: a session relay (claude, dsh) is minted by the controller's own
    # request and leaves `relay_generation` at zero.
    with db.write_transaction() as conn:
        require_native(conn, incarnation)
        lease = lock_lease(conn, lease_id)
        if lease["agent_id"] != incarnation.agent_id or lease["status"] not in (
            ImpersonationStatus.ACCEPTED,
            ImpersonationStatus.ACTIVE,
        ):
            raise ImpersonationError("Relay provisioning requires the native-held lease")
        if (lease["accepted_generation"], lease["accepted_owner"]) != (
            incarnation.generation,
            incarnation.owner,
        ):
            raise ImpersonationError("Relay provisioning belongs to the accepting incarnation")
        if (lease["relay_minted_generation"], lease["relay_minted_owner"]) == (
            incarnation.generation,
            incarnation.owner,
        ):
            return None
        fresh = conn.execute("SELECT %s > clock_timestamp()", (lease["expires_at"],)).fetchone()
        if fresh != (True,):
            return None
        if lease["relay_minted_generation"] is not None and _carrier_beats(conn, lease):
            return RelayProvision(RelayAction.ADOPTED, public(lease))
        conn.execute(
            "UPDATE agent_impersonations SET relay_token_hash=%s,relay_generation=relay_generation+1,"
            "relay_heartbeat_at=NULL,"
            "relay_minted_at=clock_timestamp(),relay_minted_generation=%s,"
            "relay_minted_owner=%s WHERE id=%s",
            (token_hash(relay_token), incarnation.generation, incarnation.owner, lease_id),
        )
        return RelayProvision(RelayAction.MINTED, public(lock_lease(conn, lease_id)))


def _carrier_beats(conn: SyncConnection, lease: Mapping[str, object]) -> bool:
    # meiva:note
    # Whether the credential another incarnation minted still has a beating carrier. The
    # database clock decides, as it does for every other lease deadline: the row is locked
    # by the caller, so this reads the heartbeat this decision is made on.
    row = conn.execute(
        "SELECT %s IS NOT NULL AND %s >= clock_timestamp() - %s*interval '1 second'",
        (lease["relay_heartbeat_at"], lease["relay_heartbeat_at"], RELAY_HEARTBEAT_STALE_SECONDS),
    ).fetchone()
    return row == (True,)


def record_degradation(
    db: DatabaseLike, lease_id: str, incarnation: RuntimeIncarnation, reason: str
) -> None:
    """Record the last observed degradation and its time, not a current health verdict."""
    with db.write_transaction() as conn:
        require_native(conn, incarnation)
        lease = lock_lease(conn, lease_id)
        if lease["agent_id"] != incarnation.agent_id or lease["status"] not in OPEN:
            return
        conn.execute(
            "UPDATE agent_impersonations SET relay_degraded_reason=%s,"
            "relay_degraded_at=clock_timestamp() WHERE id=%s",
            (reason, lease_id),
        )


def fail_acceptance(
    db: DatabaseLike, bus: EventBus, lease_id: str, incarnation: RuntimeIncarnation, reason: str
) -> dict[str, Any]:
    """Relay establishment failed: the takeover does not stand.

    Terminal 'rejected' carries the reason; a system note tells the native
    agent its acceptance was rolled back and it keeps running. Only the
    accepting native runtime may fail its own acceptance.
    """

    if not reason.strip():
        raise ValueError("A nonempty failure reason is required")
    with db.write_transaction() as conn:
        require_native(conn, incarnation)
        lease = lock_lease(conn, lease_id)
        if (
            lease["agent_id"] != incarnation.agent_id
            or lease["status"] != ImpersonationStatus.ACCEPTED
        ):
            raise ImpersonationError("Only the accepted native agent can fail relay establishment")
        if (lease["accepted_generation"], lease["accepted_owner"]) != (
            incarnation.generation,
            incarnation.owner,
        ):
            raise ImpersonationError("Acceptance belongs to another native incarnation")
        conn.execute(
            "INSERT INTO inbound_messages(agent_id,content,kind,source,payload) "
            "VALUES(%s,%s,'system_note','system:impersonation',%s)",
            (
                lease["agent_id"],
                f"Impersonation takeover {lease['id']} was rolled back: {reason} "
                "You remain the native agent; no external controller was admitted.",
                Jsonb(
                    caller_payload(
                        "system:impersonation",
                        {"impersonation_id": str(lease["id"]), "note_tag": "impersonation"},
                    )
                ),
            ),
        )
        conn.execute(
            "UPDATE agent_impersonations SET status='rejected',ended_at=clock_timestamp(),"
            "rejection_reason=%s,relay_last_failure_at=clock_timestamp() WHERE id=%s",
            (reason, lease_id),
        )
        result = public(lock_lease(conn, lease_id))
    wake_agent(db, bus, lease["agent_id"], roster_changed=True)
    logger.error(
        "impersonation relay establishment failed; takeover rolled back",
        agent_id=lease["agent_id"],
        lease_id=str(lease_id),
        reason=reason,
    )
    return result


def record_relay_failure(db: DatabaseLike, lease_id: str, incarnation: RuntimeIncarnation) -> bool:
    """Rate-limited durable stamp that relay supervision noticed a stale relay."""
    with db.write_transaction() as conn:
        require_native(conn, incarnation)
        lease = lock_lease(conn, lease_id)
        if (
            lease["agent_id"] != incarnation.agent_id
            or lease["status"] != ImpersonationStatus.ACTIVE
        ):
            raise ImpersonationError("Relay failure stamps require the active native-held lease")
        row = conn.execute(
            "UPDATE agent_impersonations SET relay_last_failure_at=clock_timestamp() "
            "WHERE id=%s AND (relay_last_failure_at IS NULL OR relay_last_failure_at < "
            "clock_timestamp() - %s*interval '1 second') RETURNING id",
            (lease_id, RELAY_FAILURE_STAMP_SECONDS),
        ).fetchone()
    return row is not None


ABORTED_REASON_PREFIX = "aborted: "


def aborted_detail(reason: object) -> str | None:
    """The component-death detail of a supervisor-aborted lease, else None."""
    if not isinstance(reason, str) or not reason.startswith(ABORTED_REASON_PREFIX):
        return None
    return reason.removeprefix(ABORTED_REASON_PREFIX).strip() or None


def abort_lease(
    db: DatabaseLike, bus: EventBus, lease_id: str, incarnation: RuntimeIncarnation, detail: str
) -> dict[str, Any] | None:
    # meiva:note
    # Stop a takeover after confirmed executor death, like a TTL expiry.
    #
    # ``detail`` is the confirmed executor death phrase ("the executor process
    # is gone"). The lease goes terminal ("expired")
    # with ``rejection_reason`` recorded as ``aborted: <detail>`` — the request's
    # own ``reason`` (its stated purpose) is preserved, and the resume chain
    # reads the prefixed marker back via ``aborted_detail``. Like expiry, a
    # log-native lease closes event admission so its log can complete. The checkpoint
    # resume chain delivers the end note; pending renewal reminders are dismissed.
    # Idempotent: an already-terminal lease returns None. Every writer's lease
    # lock serializes with claim-time expiry, the TTL reaper and the terminate
    # trigger, so an abort that loses the race is a no-op here.
    from base.agents.impersonation._store import close_event_admission
    from base.agents.impersonation.status import is_log_native

    detail = detail.strip()
    if not detail:
        raise ValueError("A nonempty abort detail is required")
    reason = f"{ABORTED_REASON_PREFIX}{detail}"
    with db.write_transaction() as conn:
        require_native(conn, incarnation)
        lease = lock_lease(conn, lease_id)
        if lease["agent_id"] != incarnation.agent_id:
            raise ImpersonationError("Lease abort requires the native-held lease")
        if lease["status"] not in OPEN:
            return None
        if is_log_native(lease):
            close_event_admission(conn, lease_id)
        conn.execute(
            "UPDATE agent_impersonations SET status='expired',ended_at=clock_timestamp(),"
            "rejection_reason=%s WHERE id=%s",
            (reason, lease_id),
        )
        dismiss_reminders(conn, lease)
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT * FROM agent_impersonations WHERE id=%s", (lease_id,))
            ended = cur.fetchone()
            assert ended is not None  # noqa: S101 — locked overhead row exists
    wake_agent(db, bus, lease["agent_id"], roster_changed=True)
    return public(parse_lease(ended))


def stamp_relay_failure(
    db: DatabaseLike,
    session: dict[str, Any],
    agent_id: int,
    *,
    incarnation: RuntimeIncarnation | None,
) -> None:
    if incarnation is None:
        return
    try:
        stamped = record_relay_failure(db, session["id"], incarnation)
    except (ImpersonationError, RuntimeError):
        return
    if stamped:
        logger.error(
            "impersonation relay heartbeat stale; automatic recovery is not provided",
            agent_id=agent_id,
            lease_id=str(session["id"]),
            relay_provider=session["relay_provider"],
        )

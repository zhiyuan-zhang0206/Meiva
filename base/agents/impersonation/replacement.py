# meiva:note
# Carry one takeover across the admission that replaces its native incarnation.
#
# A lease belongs to the agent, not to the process that accepted it. The admission
# transaction moves the recorded accepting incarnation to the admitted one, so the takeover
# continues under it: the credential, the deadline, the event log admission and the status
# all stay, and the external controller keeps working without a new request. The move is
# recorded in the lease's own log (agent_impersonation_entries, kind lifecycle), because the
# accepting incarnation is not otherwise recoverable — the row only ever names the
# incarnation that carries the lease now. A successor that cannot make the move falls back
# to `end_for_replacement`, the pre-carry behaviour, so an admission never refuses and never
# leaves a half-moved lease behind.

from dataclasses import dataclass
from uuid import UUID

from psycopg.types.json import Jsonb

from base.agents.impersonation.status import is_log_native
from base.db.protocols import AsyncConnectionLike
from base.native_process.runtime_incarnation import RuntimeIncarnation

REPLACED_DETAIL = "native runtime restarted"


@dataclass(frozen=True)
class Adoption:
    lease_id: str
    session_id: int
    status: str
    accepted_from: tuple[UUID, UUID]
    accepted_by: tuple[UUID, UUID]
    relay_provider: str | None


async def adopt_for_successor(
    conn: AsyncConnectionLike, agent_id: int, incarnation: RuntimeIncarnation
) -> Adoption | None:
    # meiva:note
    # Move the agent's accepted or active lease to the admitted incarnation.
    #
    # Runs in the admission transaction, after the agent row names `incarnation`, so
    # every native ownership check of the live process agrees with the lease it carries.
    # Returns None when no open lease is bound to another incarnation: a merely requested
    # lease has no binding, and a lease this incarnation already carries needs no move.
    # meiva:link base/agents/impersonation/docs/ownership.meiva.okf.md
    cursor = await conn.execute(
        "SELECT id,session_id,status,accepted_generation,accepted_owner,relay_provider "
        "FROM agent_impersonations "
        "WHERE agent_id=%s AND status IN ('accepted','active') "
        "AND (accepted_generation,accepted_owner) IS DISTINCT FROM (%s,%s) "
        "ORDER BY created_at LIMIT 1 FOR UPDATE",
        (agent_id, incarnation.generation, incarnation.owner),
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    lease_id, session_id, status, from_generation, from_owner, provider = row
    await conn.execute(
        "UPDATE agent_impersonations SET accepted_generation=%s,accepted_owner=%s WHERE id=%s",
        (incarnation.generation, incarnation.owner, lease_id),
    )
    await _record_adoption(
        conn,
        str(lease_id),
        accepted_from=(from_generation, from_owner),
        accepted_by=(incarnation.generation, incarnation.owner),
        provider=provider,
    )
    return Adoption(
        lease_id=str(lease_id),
        session_id=session_id,
        status=status,
        accepted_from=(from_generation, from_owner),
        accepted_by=(incarnation.generation, incarnation.owner),
        relay_provider=provider,
    )


async def _record_adoption(
    conn: AsyncConnectionLike,
    lease_id: str,
    *,
    accepted_from: tuple[UUID, UUID],
    accepted_by: tuple[UUID, UUID],
    provider: str | None,
) -> None:
    # meiva:note
    # Append the move to the lease's own log, under the row lock the caller holds.
    # `history.append` is the sync twin of these two statements; the admission holds an
    # async connection. The entry carries no event identity: a repeated admission finds the
    # recorded pair already equal and returns before reaching this point.
    row = await (
        await conn.execute(
            "UPDATE agent_impersonations SET next_entry=next_entry+1 WHERE id=%s "
            "RETURNING next_entry-1",
            (lease_id,),
        )
    ).fetchone()
    if row is None:
        raise RuntimeError("Adopted lease disappeared")
    await conn.execute(
        "INSERT INTO agent_impersonation_entries(lease_id,seq,kind,payload) "
        "VALUES(%s,%s,'lifecycle',%s)",
        (
            lease_id,
            row[0],
            Jsonb(
                {
                    "event": "lease_adopted",
                    "from": {"generation": str(accepted_from[0]), "owner": str(accepted_from[1])},
                    "to": {"generation": str(accepted_by[0]), "owner": str(accepted_by[1])},
                    "relay_provider": provider,
                }
            ),
        ),
    )


async def end_for_replacement(
    conn: AsyncConnectionLike, agent_id: int, incarnation: RuntimeIncarnation
) -> str | None:
    # meiva:note
    # Close the agent's accepted or active lease if the admitted incarnation cannot carry it.
    #
    # This is the fallback that keeps an admission from ever refusing or stalling: the
    # caller runs it when adoption raises, and it also runs for a lease adoption refuses
    # to move. The accepting binding is first moved to the admitted incarnation: closing a
    # lease restores the recorded accepting incarnation into the agent row
    # (`restore_native_impersonation_owner`), and restoring the dead predecessor would
    # break every native ownership check of the live process. Returns the closed lease id,
    # or None when nothing was closed.
    cursor = await conn.execute(
        "SELECT id,event_delivery_protocol_version FROM agent_impersonations "
        "WHERE agent_id=%s AND status IN ('accepted','active') "
        "AND (accepted_generation,accepted_owner) IS DISTINCT FROM (%s,%s) "
        "ORDER BY created_at LIMIT 1 FOR UPDATE",
        (agent_id, incarnation.generation, incarnation.owner),
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    lease_id, protocol = row
    await conn.execute(
        "UPDATE agent_impersonations SET accepted_generation=%s,accepted_owner=%s WHERE id=%s",
        (incarnation.generation, incarnation.owner, lease_id),
    )
    if is_log_native({"event_delivery_protocol_version": protocol}):
        await conn.execute("SELECT close_impersonation_event_admission(%s)", (lease_id,))
    await conn.execute(
        "UPDATE agent_impersonations SET status='expired',ended_at=clock_timestamp(),"
        "rejection_reason=%s WHERE id=%s",
        (f"aborted: {REPLACED_DETAIL}", lease_id),
    )
    await conn.execute(
        "UPDATE inbound_messages SET status='done' "
        "WHERE agent_id=%s AND kind='reminder' AND status='pending' AND payload->>'lease_id'=%s",
        (agent_id, str(lease_id)),
    )
    return str(lease_id)

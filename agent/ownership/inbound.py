"""Serialize queue mutation with the admitted consumer's runtime authority."""

import psycopg

from base.native_process.runtime_incarnation import RuntimeIncarnation


class RuntimeOwnershipLostError(RuntimeError):
    """The caller must stop without changing a replacement runtime's state."""


# meiva:note
# Lock agents_meta before inbound rows in the caller's write transaction.
#
# An unowned row (no generation and no owner) accepts the missing token, and an
# owned row never does: the caller must hold the row's exact generation and
# owner. The lock serializes admission with claiming.
async def lock_inbound_owner(
    conn: psycopg.AsyncConnection, agent_id: int, *, incarnation: RuntimeIncarnation | None
) -> None:
    if incarnation is not None:
        incarnation.require_agent(agent_id)
    cursor = await conn.execute(
        "SELECT runtime_generation, runtime_owner, status FROM agents_meta "
        "WHERE id = %s FOR UPDATE",
        (agent_id,),
    )
    row = await cursor.fetchone()
    if row is not None:
        generation, owner, status = row
        if incarnation is None:
            if generation is None and owner is None:
                return
        elif (
            generation == incarnation.generation
            and owner == incarnation.owner
            and status in ("running", "idling")
        ):
            return
    raise RuntimeOwnershipLostError(f"agent {agent_id} no longer owns its runtime row")

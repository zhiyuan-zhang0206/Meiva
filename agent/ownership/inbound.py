# meiva:note
# Serialize queue mutation with the admitted consumer's runtime authority.

from agent.ownership.lifecycle_plan import (
    RuntimeOwnershipLostError as RuntimeOwnershipLostError,
)
from agent.ownership.lifecycle_plan import (
    owns_inbound_row,
)
from base.db.protocols import AsyncConnectionLike
from base.native_process.runtime_incarnation import RuntimeIncarnation


# meiva:note
# Lock agents_meta before inbound rows in the caller's write transaction.
#
# An unowned row (no generation and no owner) accepts the missing token, and an
# owned row never does: the caller must hold the row's exact generation and
# owner. The lock serializes admission with claiming.
async def lock_inbound_owner(
    conn: AsyncConnectionLike, agent_id: int, *, incarnation: RuntimeIncarnation | None
) -> None:
    if incarnation is not None:
        incarnation.require_agent(agent_id)
    cursor = await conn.execute(
        "SELECT runtime_generation, runtime_owner, status FROM agents_meta "
        "WHERE id = %s FOR UPDATE",
        (agent_id,),
    )
    row = await cursor.fetchone()
    if owns_inbound_row(row, incarnation=incarnation):
        return
    raise RuntimeOwnershipLostError(f"agent {agent_id} no longer owns its runtime row")

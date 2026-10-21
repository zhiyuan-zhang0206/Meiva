"""End a takeover that a replacement native incarnation inherits, in its admission.

A lease is bound to the native incarnation that accepted it. A hosted restart or
host takeover admits a different incarnation, and the lease does not follow it: the
admission transaction closes any accepted or active lease as expired, with the
abort reason below, and the replacement resumes through the ordinary return chain.
An external controller must request again.
"""

from typing import Any

import psycopg

from base.agents.impersonation.status import is_log_native
from base.native_process.runtime_incarnation import RuntimeIncarnation

REPLACED_DETAIL = "native runtime restarted"


async def end_for_replacement(
    conn: psycopg.AsyncConnection[Any], agent_id: int, incarnation: RuntimeIncarnation
) -> str | None:
    # meiva:note
    # Close the agent's accepted or active lease if another incarnation accepted it.
    #
    # Runs in the admission transaction, after the agent row names `incarnation`.
    # The accepting binding is first moved to the admitted incarnation: closing a
    # lease restores the recorded accepting incarnation into the agent row
    # (`restore_native_impersonation_owner`), and restoring the dead predecessor
    # would break every native ownership check of the live process. Returns the
    # closed lease id, or None when nothing was closed. A merely requested lease has
    # no binding and stays open for the replacement to prepare.
    cursor = await conn.execute(
        "SELECT id,event_delivery_protocol_version "
        "FROM agent_impersonations "
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

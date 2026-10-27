# meiva:note
# One permanent provider rejection stops background wakes. A fresh chat, an operator restart or
# explicit operator resurrection may try again after a fix; successful model progress clears the
# durable rejection. No billing batch recovery is provided.

from __future__ import annotations

from typing import Final, LiteralString

import psycopg
from psycopg_pool import AsyncConnectionPool

from base.db.protocols import AsyncConnectionLike, AsyncPool
from base.db.transaction import async_write_transaction

HALT_AFTER_CONSECUTIVE_PERMANENT_REJECTS = 1

SUPPRESS_REASON_PERMANENT_REJECT = "permanent_provider_reject"

# The tripped window is "until human" in practice: long enough that no
# automatic path resumes on a mere timer, finite so the column stays
# representable everywhere (no `infinity` datetime edge cases).
HALT_SUPPRESSION_WINDOW_S = 3650 * 24 * 3600.0

# Stored reason for an HTTP 402 billing rejection, shared with the classifier.
PERMANENT_REJECT_REASON_BILLING: Final = "billing"

# Correlated against the unaliased agents_meta row. This is the SQL half of the
# breaker the automatic-recovery gates embed; keep the literal
# in sync with HALT_AFTER_CONSECUTIVE_PERMANENT_REJECTS (asserted by
# tests/components/base/test_recovery_breaker.py).
RECOVERY_BREAKER_CLEAR: LiteralString = "permanent_reject_streak < 1"


async def record_permanent_reject_turn(
    pool: AsyncConnectionPool, agent_id: int, reason: str
) -> int:
    """Count one permanent-class rejected turn; return the new streak.

    `reason` is the ``_circuit_reason`` class of THIS rejection; it is
    recorded beside the streak (``last_permanent_reject_reason``) so durable
    diagnostics can identify the cause of the halt. Callers keep this
    best-effort (log-and-continue on failure): the breaker must never mask
    the original provider rejection.
    """
    async with async_write_transaction(pool) as conn, conn.cursor() as cur:
        await cur.execute(
            "UPDATE agents_meta SET permanent_reject_streak = permanent_reject_streak + 1, "
            "last_permanent_reject_reason = %s "
            "WHERE id = %s RETURNING permanent_reject_streak",
            (reason, agent_id),
        )
        row = await cur.fetchone()
    if row is None:
        raise RuntimeError(f"agent {agent_id} disappeared while recording a permanent rejection")
    return int(row[0])


async def halt_automatic_recovery(pool: AsyncConnectionPool, agent_id: int) -> bool:
    """Trip the breaker: suppress automatic wakes for `agent_id`.

    Idempotent against an already-tripped row: while the permanent-reject
    suppression is active it is left untouched (the streak is the durable
    gate; this write is the operator-visible reason and the input to the
    suppression gates). Any other reason's window — or an expired one — is
    replaced, because this trip is the stronger, until-human state.

    Returns True when this call wrote a fresh suppression window.
    """
    async with async_write_transaction(pool) as conn, conn.cursor() as cur:
        await cur.execute(
            "UPDATE agents_meta "
            "SET wake_suppressed_until = clock_timestamp() + make_interval(secs => %s), "
            "    wake_suppress_reason = %s "
            "WHERE id = %s "
            "  AND (wake_suppress_reason IS DISTINCT FROM %s "
            "       OR wake_suppressed_until IS NULL "
            "       OR wake_suppressed_until < now())",
            (
                HALT_SUPPRESSION_WINDOW_S,
                SUPPRESS_REASON_PERMANENT_REJECT,
                agent_id,
                SUPPRESS_REASON_PERMANENT_REJECT,
            ),
        )
        return cur.rowcount == 1


# meiva:note
# The operator lifecycle command that may still open a turn while the breaker is tripped.
#
# `restart` is the operator's own repair — the frontend button and the CLI write source
# `"user"`, an SDK caller writes `agent:<id>` — and it arrives *pending*, waiting for
# exactly the admission this gate refuses. Requiring it to be claimed first was a fixed
# point: admission refused, so the row was never claimed, so the claimed clause never
# held (2026-10-27, agent 405: `meiva agents restart` enqueued and never ran while a
# 3650-day window sat there). A self-restart (`source='self'`, the claim node's own
# re-entry) and a system notice stay blocked: those are the automatic loops the breaker
# exists to stop, and admitting them would let the agent clear its own halt.
#
# Asked only on the halted path, so a healthy agent pays no extra query.
async def _operator_restart_waiting(conn: AsyncConnectionLike, agent_id: int) -> bool:
    row = await (
        await conn.execute(
            "SELECT EXISTS (SELECT 1 FROM inbound_messages WHERE agent_id=%s "
            "AND kind='restart' AND status IN ('pending','claimed') "
            "AND source <> 'self' AND source NOT LIKE 'system%%')",
            (agent_id,),
        )
    ).fetchone()
    return bool(row[0]) if row is not None else False


# meiva:note
# Keep background inputs halted; a fresh peer/user chat or an operator restart can retry.
#
# The grant does not clear the streak: only a turn that actually makes progress does
# (`agent/graph/claim/turn_stamp.py::persist_last_active`), so the breaker keeps its meaning —
# the halt ends when the model works again, not when someone pokes it.
async def admit_pending_wake(pool: AsyncPool, agent_id: int) -> bool:
    async with async_write_transaction(pool) as conn:
        row = await (
            await conn.execute(
                "SELECT permanent_reject_streak,EXISTS (SELECT 1 FROM inbound_messages "
                "WHERE agent_id=agents_meta.id AND kind IN ('restart','terminate') "
                "AND status='claimed') FROM agents_meta WHERE id=%s FOR UPDATE",
                (agent_id,),
            )
        ).fetchone()
        if row is None:
            return False
        if row[0] < HALT_AFTER_CONSECUTIVE_PERMANENT_REJECTS or row[1]:
            return True
        if await _operator_restart_waiting(conn, agent_id):
            return True
        fresh = await (
            await conn.execute(
                "SELECT 1 FROM inbound_messages i JOIN agents_meta m ON m.id=i.agent_id "
                "WHERE i.agent_id=%s AND i.status='pending' AND i.kind='chat' "
                "AND i.source<>'system' AND i.source NOT LIKE 'system:%%' "
                "AND i.created_at>COALESCE(m.last_turn_fatal_at,m.status_changed_at) LIMIT 1",
                (agent_id,),
            )
        ).fetchone()
        if fresh is None:
            return False
        await conn.execute(
            "UPDATE agents_meta SET permanent_reject_streak=0,last_permanent_reject_reason=NULL,"
            "wake_suppressed_until=NULL,wake_suppress_reason=NULL WHERE id=%s",
            (agent_id,),
        )
        return True


# meiva:note
# Clear the halt: the streak, the reason and the "until human" window this module wrote.
#
# One owner, because the three must leave together. While they could drift apart, a completed
# turn cleared the streak and left a 3650-day window behind (#5667): the breaker then read
# clear while every window reader stayed suppressed, and nothing in the system would have
# reported it. The window is only cleared when the reason is this module's own — the
# stranded-wake cooldown is another writer's state on the same column and must survive.
async def clear_permanent_reject_halt(cur: psycopg.AsyncCursor, agent_id: int) -> None:
    await cur.execute(
        "UPDATE agents_meta SET permanent_reject_streak = 0, "
        "last_permanent_reject_reason = NULL, "
        "wake_suppressed_until = CASE WHEN wake_suppress_reason = %s THEN NULL "
        "ELSE wake_suppressed_until END, "
        "wake_suppress_reason = CASE WHEN wake_suppress_reason = %s THEN NULL "
        "ELSE wake_suppress_reason END "
        "WHERE id = %s",
        (SUPPRESS_REASON_PERMANENT_REJECT, SUPPRESS_REASON_PERMANENT_REJECT, agent_id),
    )

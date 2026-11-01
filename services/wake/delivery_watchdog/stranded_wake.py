# meiva:note
# The delivery fallback for a message whose owner died before claiming it.
#
# Delivery is the primary wake: `ops.lifecycle.deliver_chat_inbound` runs
# `resurrect_if_terminated` at write time, so a message written to an already
# terminated owner wakes it. That call cannot fire for the other order — the
# message commits while the owner still reads `running`, the owner finishes dying
# before its claim reaches the row, and the row keeps `pending` with no live
# consumer. The agent host polls only `idling`/`running` owners
# (`scheduling/pending_wakes.py::scan_rows`), so nothing else looks at it: the row
# then waits for the next message to the same owner or for the dead-letter sweep
# 24h later (2026-10-25, inbound 345099 to agent 6984, 67 minutes).
#
# This module is that fallback and only that: it never runs before the delivery
# path, it runs the delivery path's own wake, and each throttle below exists
# because a wake is a launch RPC to the owner's home machine.
# meiva:link docs/decisions/agents/messages/2026-10-25-stranded-inbound-wake-fallback.md

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import LiteralString

from psycopg import sql
from psycopg_pool import ConnectionPool

from base import telemetry
from base.agents.incarnation.lifecycle_commands import (
    SYSTEM_NOTICE_SOURCE,
    TRIGGER_AFTER_KILL_COMMAND,
)
from base.agents.messages.inbound import WakeTriggerKind, validate_wake_trigger_kind
from base.agents.recovery.breaker import RECOVERY_BREAKER_CLEAR, SUPPRESS_REASON_PERMANENT_REJECT
from base.api_contracts.agent_contract import AgentStatus
from base.db import Database
from base.db.transaction import write_transaction
from base.events.live.bus import EventBus
from ops.cluster.rpc import RpcClient

_log = logging.getLogger("services.wake.delivery_watchdog.stranded_wake")

# meiva:note
# The cooldown this loop stamps carries its own reason: an operator reading
# `agents_meta.wake_suppress_reason` must be able to tell a stranded-wake window
# from the until-human permanent-reject halt.
STRANDED_WAKE_REASON = "stranded_wake"

# meiva:note
# What counts as work that may wake a dead owner: a chat, or a task direction —
# the payload predicate `supersede_task_assignments` settles unclaimed directions
# by, so the two agree on what a direction is.
_STRANDED_KIND: LiteralString = (
    "(m.kind = 'chat' OR (m.kind = 'system_note' "
    "AND m.payload->'task_notification' = 'true'::jsonb "
    "AND m.payload->'delivery_resurrect' = 'true'::jsonb))"
)

# meiva:note
# The wake this loop runs: production passes `wake_stranded`, and a test passes
# its own, so the round's throttle is testable without patching this module.
WakeStranded = Callable[[Database, EventBus, "StrandedOwner", str], Awaitable[bool]]


# meiva:note
# One terminated owner and the exact pending row the wake is justified by; the
# row is the home-side CAS's evidence, not a hint.
@dataclass(frozen=True)
class StrandedOwner:
    agent_id: int
    trigger_inbound_id: int
    trigger_kind: WakeTriggerKind


# meiva:note
# One row per terminated owner: the oldest pending row that passes the shared
# wake fence (mandatory — an involuntary death is never woken for leftover work),
# bounded by `age_bound_s` — the same clock the dead-letter sweep
# retires the row on, so the fallback stops exactly where retirement takes over.
# The gates it adds to the fence are its own policy: the recovery breaker and its
# own cooldown exclude an owner, because an until-human halt is not resumed by a
# timer and a second attempt inside the cooldown is the burst this loop exists to
# avoid. The two are read separately — the breaker clause judges the halt, this
# loop's cooldown window judges the backoff — because `wake_suppressed_until` is
# one column with two writers (2026-10-27, #5667): a breaker window is "until
# human" and is not this loop's business, so it never suppresses a stranded wake
# whose own repair already ran. A system notice queued for a dead owner is read on
# a later life and never wakes one.
#
# meiva:note
# Concurrency safety: SELECT DISTINCT ON precludes `FOR UPDATE SKIP LOCKED` in Postgres
# (FeatureNotSupported: FOR UPDATE is not allowed with DISTINCT clause). `select_stranded`
# is a read-only candidate pass; concurrency is serialized downstream by `claim_stranded`'s
# atomic UPDATE ... RETURNING id.
def select_stranded(pool: ConnectionPool, age_bound_s: float) -> list[StrandedOwner]:
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            sql.SQL(
                "SELECT DISTINCT ON (m.agent_id) m.agent_id, m.id, m.kind "
                "FROM inbound_messages m "
                "JOIN agents_meta ON agents_meta.id = m.agent_id "
                "WHERE agents_meta.status = 'terminated' "
                "  AND m.status = 'pending' "
                "  AND {kind} "
                "  AND {fence} "
                "  AND m.created_at > now() - make_interval(secs => %s) "
                "  AND NOT {notice} "
                "  AND (agents_meta.wake_suppressed_until IS NULL "
                "       OR agents_meta.wake_suppressed_until < now() "
                "       OR agents_meta.wake_suppress_reason = {breaker_reason}) "
                "  AND {breaker} "
                "ORDER BY m.agent_id, m.id"
            ).format(
                kind=sql.SQL(_STRANDED_KIND),
                fence=sql.SQL(TRIGGER_AFTER_KILL_COMMAND),
                notice=sql.SQL(SYSTEM_NOTICE_SOURCE),
                breaker=sql.SQL(RECOVERY_BREAKER_CLEAR),
                breaker_reason=sql.Literal(SUPPRESS_REASON_PERMANENT_REJECT),
            ),
            (age_bound_s,),
        )
        return [
            StrandedOwner(int(agent_id), int(inbound_id), validate_wake_trigger_kind(str(kind)))
            for agent_id, inbound_id, kind in cur.fetchall()
        ]


# meiva:note
# The attempt claim is the cooldown: one atomic UPDATE stamps the window on
# exactly the owners it selects, so two rounds (or two daemons) can never attempt
# the same owner twice inside a window, a restart resumes the clock, and three
# queued messages still mean one attempt. `max_per_round` caps a fleet-wide
# die-off to a drain over rounds instead of a burst of launch RPCs, and the
# until-human halt is never replaced by this window.
def claim_stranded(
    pool: ConnectionPool,
    agent_ids: list[int],
    cooldown_s: float,
    max_per_round: int,
) -> list[int]:
    if not agent_ids or max_per_round < 1:
        return []
    with write_transaction(pool) as conn, conn.cursor() as cur:
        cur.execute(
            sql.SQL(
                "UPDATE agents_meta "
                "SET wake_suppressed_until = clock_timestamp() + make_interval(secs => %s), "
                "    wake_suppress_reason = %s "
                "WHERE id IN ("
                "  SELECT id FROM agents_meta "
                "  WHERE id = ANY(%s) AND status = 'terminated' "
                "    AND (wake_suppressed_until IS NULL OR wake_suppressed_until < now()) "
                "    AND wake_suppress_reason IS DISTINCT FROM %s "
                "    AND {breaker} "
                "  ORDER BY id LIMIT %s"
                ") RETURNING id"
            ).format(breaker=sql.SQL(RECOVERY_BREAKER_CLEAR)),
            (
                cooldown_s,
                STRANDED_WAKE_REASON,
                agent_ids,
                SUPPRESS_REASON_PERMANENT_REJECT,
                max_per_round,
            ),
        )
        return sorted(int(row[0]) for row in cur.fetchall())


# meiva:note
# One wake per claimed owner, through the delivery path's own op: placement, the
# pause latch, the recovery breaker, the notice refusal and the home-side CAS all
# stay in `ops.lifecycle.resurrect_if_terminated`. A failure leaves the row
# pending and the cooldown standing, so the next round retries until the age bound
# retires it; a refused wake (still terminated) is not a wake and does not claim
# the recovery fact.
async def wake_stranded(
    db: Database,
    bus: EventBus,
    owner: StrandedOwner,
    local_machine: str,
    *,
    rpc: RpcClient,
) -> bool:
    from ops.lifecycle import resurrect_if_terminated

    try:
        status = await resurrect_if_terminated(
            db,
            bus,
            owner.agent_id,
            trigger_inbound_id=owner.trigger_inbound_id,
            trigger_inbound_kind=owner.trigger_kind,
            local_machine=local_machine,
            rpc=rpc,
        )
    except Exception:
        _log.warning(
            "[delivery] stranded wake failed for agent %s (inbound %s)",
            owner.agent_id,
            owner.trigger_inbound_id,
            exc_info=True,
        )
        return False
    if status is AgentStatus.TERMINATED:
        _log.info(
            "[delivery] stranded wake for agent %s (inbound %s) refused — still terminated",
            owner.agent_id,
            owner.trigger_inbound_id,
        )
        return False
    _log.info(
        "[delivery] stranded wake for agent %s (inbound %s) -> %s",
        owner.agent_id,
        owner.trigger_inbound_id,
        status,
    )
    _emit_stranded_wake(owner)
    return True


# meiva:note
# The recovery fact of the fallback, with the stall alert beside it: together they
# tell an operator whether a delivery was merely late or had no consumer at all.
# An emit failure is logged, never raised.
def _emit_stranded_wake(owner: StrandedOwner) -> None:
    try:
        telemetry.emit(
            "telemetry",
            "delivery_stranded_wake",
            level="warning",
            agent_id=owner.agent_id,
            source="system",
            attributes={"inbound_id": owner.trigger_inbound_id, "kind": owner.trigger_kind},
        )
    except Exception:
        _log.exception(
            "[delivery] delivery_stranded_wake emit failed for inbound %s",
            owner.trigger_inbound_id,
        )


# meiva:note
# One round: select, claim, then wake the claimed owners one at a time. The wake
# is an explicit parameter so a test passes its own, and the round returns the
# number of attempts for the loop's log and tests.
async def stranded_wake_round(
    pool: ConnectionPool,
    db: Database,
    bus: EventBus,
    local_machine: str,
    *,
    age_bound_s: float,
    cooldown_s: float,
    max_per_round: int,
    wake: WakeStranded,
) -> int:
    stranded = await asyncio.to_thread(select_stranded, pool, age_bound_s)
    if not stranded:
        return 0
    trigger_of = {owner.agent_id: owner for owner in stranded}
    claimed = await asyncio.to_thread(
        claim_stranded, pool, list(trigger_of), cooldown_s, max_per_round
    )
    deferred = len(trigger_of) - len(claimed)
    if deferred > 0:
        _log.info("[delivery] stranded wake backlog: %s owner(s) deferred", deferred)
    for agent_id in claimed:
        await wake(db, bus, trigger_of[agent_id], local_machine)
    return len(claimed)

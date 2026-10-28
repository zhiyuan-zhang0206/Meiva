# meiva:note
# Hosted incarnation admission and settlement, replacing status-only writes.

import asyncio
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import assert_never
from uuid import UUID

from loguru import logger

from agent.ownership.audit_payloads import (
    AdoptedLease,
    admitted_payload,
    adopted_payload,
    settled_payload,
)
from agent.ownership.lifecycle_plan import (
    AppliedLifecycle as AppliedLifecycle,
)
from agent.ownership.lifecycle_plan import (
    LifecycleKind as LifecycleKind,
)
from agent.ownership.lifecycle_plan import (
    TurnFatalStamp as TurnFatalStamp,
)
from agent.ownership.lifecycle_plan import (
    TurnSettlement as TurnSettlement,
)
from agent.ownership.lifecycle_plan import (
    lifecycle_kind,
    resume_note,
    shell_cutoff,
    turn_close,
)
from base.agents.impersonation.replacement import (
    Adoption,
    adopt_for_successor,
    end_for_replacement,
)
from base.db.protocols import AsyncConnectionLike, AsyncPool, DatabaseLike
from base.db.transaction import async_write_transaction
from base.events.live.announce import publish_agent_updated
from base.events.live.bus import EventBus
from base.native_process.runtime_incarnation import RuntimeIncarnation
from base.telemetry import Event, emit_prepared
from base.telemetry.audit_events import prepare_event_log, record_audit_async


# meiva:note
# Execute the claimed restart/terminate command and mark it done, in one transaction.
#
#     Called after the existing single-flight continuation has safely ended. The
#     agent row is locked and fenced by this incarnation's generation and owner;
#     the oldest claimed command is then executed and closed in the same
#     transaction, so no state exists in which it is half applied. Restart
#     releases ownership, so the next admission creates a new incarnation.
#     Terminate keeps the runtime identity and closes every open terminate of the
#     agent, since one death serves them all. Returns what was applied, or None
#     when the incarnation no longer owns the row or no command was claimed.
#
#     `resume_turn` says the restart ended a turn that was not finished (the claim
#     that took it did not find the agent idle). The same transaction then queues
#     the one pending note that continues that turn (`resume_note`, source
#     the restart's own), so the restart is never done without the note that wakes
#     the agent: the pending scan finds it on this host or the next. An idle agent
#     gets no note, and neither does a terminate.
#
#     When a terminate asked to take the agent's shell sessions with it, the
#     row lock captures the next shell ID as `shell_cutoff`. Nothing runs after the
#     commit here: the caller executes the post-commit cleanup (the change hint, the
#     browser pages and the shell sessions older than the cutoff), so the lifecycle
#     lock is never held across it and the host reports cleanup failures without
#     rolling back the durable death. A crash or a session created after this cutoff
#     can leave live sessions for operators.
async def apply_hosted_lifecycle(
    pool: AsyncPool,
    incarnation: RuntimeIncarnation,
    *,
    resume_turn: bool,
) -> AppliedLifecycle | None:
    cutoff: int | None = None
    async with async_write_transaction(pool) as conn:
        cursor = await conn.execute(
            "SELECT session_index FROM agents_meta WHERE id=%s "
            "AND runtime_generation=%s AND runtime_owner=%s "
            "AND status IN ('running','idling') FOR UPDATE",
            (incarnation.agent_id, incarnation.generation, incarnation.owner),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        cursor = await conn.execute(
            "SELECT id,kind,source FROM inbound_messages WHERE agent_id=%s "
            "AND kind IN ('restart','terminate') AND status='claimed' "
            "ORDER BY id LIMIT 1 FOR UPDATE",
            (incarnation.agent_id,),
        )
        command = await cursor.fetchone()
        if command is None:
            return None
        command_id, command_kind, command_source = command
        kind = lifecycle_kind(command_kind)
        match kind:
            case "restart":
                await conn.execute(
                    "UPDATE agents_meta SET status='idling',runtime_generation=NULL,"
                    "runtime_owner=NULL WHERE id=%s",
                    (incarnation.agent_id,),
                )
                # meiva:note
                # The release marker is the maintenance drain's proof that this restart
                # executed, as opposed to being closed as failed or superseded.
                await conn.execute(
                    "UPDATE inbound_messages SET status='done',"
                    "payload=COALESCE(payload,'{}'::jsonb)||jsonb_build_object('lifecycle_release',true) "
                    "WHERE id=%s",
                    (command_id,),
                )
                note = resume_note(command_source, resume_turn=resume_turn)
                if note is not None:
                    await conn.execute(
                        "INSERT INTO inbound_messages(agent_id,content,kind,source,payload) "
                        "VALUES(%s,%s,'system_note',%s,jsonb_build_object('note_tag',%s::text))",
                        (
                            incarnation.agent_id,
                            note.content,
                            note.source,
                            note.note_tag,
                        ),
                    )
            case "terminate":
                closed = await conn.execute(
                    "UPDATE inbound_messages SET status='done' WHERE agent_id=%s AND kind='terminate' "
                    "AND status IN ('pending','claimed') RETURNING "
                    "COALESCE((payload -> 'kill_all_shell_sessions') = 'true'::jsonb, false)",
                    (incarnation.agent_id,),
                )
                cutoff = shell_cutoff([kill for (kill,) in await closed.fetchall()], row[0])
                await conn.execute(
                    "UPDATE agents_meta SET status='terminated',termination_source='user' WHERE id=%s",
                    (incarnation.agent_id,),
                )
            case _:
                assert_never(kind)
    return AppliedLifecycle(kind=kind, agent_id=incarnation.agent_id, shell_cutoff=cutoff)


# meiva:note
# Hand the agent's open impersonation lease to the incarnation this admission just wrote.
#
# Adoption runs in a savepoint: a lease this successor cannot carry is closed exactly as it
# was before carry existed, and the admission itself never fails because of a takeover. The
# two outcomes are exclusive by construction — a failure inside the savepoint rolls its
# writes back, so the fallback never closes a half-moved lease, and the lease is never left
# in the intermediate state "closed here, not adopted there".
async def _termination_pending(conn: AsyncConnectionLike, agent_id: int) -> bool:
    # meiva:note
    # Whether this admission is the one that will terminate the agent.
    #
    # A takeover is carried across a restart, never across a termination: the termination
    # trigger ends the lease inside the transaction that writes `terminated`, which is after
    # this admission, and an ended lease still owes its handoff to the native checkpoint
    # *before* the row goes down (`_apply_lease_controls` settles it in this wake). Ending it
    # here keeps that order.
    row = await (
        await conn.execute(
            "SELECT 1 FROM inbound_messages WHERE agent_id=%s AND kind='terminate' "
            "AND status IN ('pending','claimed') LIMIT 1",
            (agent_id,),
        )
    ).fetchone()
    return row is not None


async def _carry_impersonation(
    conn: AsyncConnectionLike, agent_id: int, incarnation: RuntimeIncarnation
) -> tuple[Adoption | None, str | None]:
    if await _termination_pending(conn, agent_id):
        return None, await end_for_replacement(conn, agent_id, incarnation)
    try:
        async with conn.transaction():
            return await adopt_for_successor(conn, agent_id, incarnation), None
    except Exception:
        logger.exception(
            "impersonation lease adoption failed; ending the inherited lease", agent_id=agent_id
        )
    return None, await end_for_replacement(conn, agent_id, incarnation)


# meiva:note
# Admit one turn: the circuit breaker, then one UPDATE.
#
#     Only this machine's running or idling row is claimed; a terminated or foreign
#     row yields None. The generation survives while the same host boot keeps the
#     row and is minted fresh when the owner differs, so a new boot, or a restart
#     that cleared the identity, starts a new incarnation. A host that is exiting
#     refuses new turns in process (`stop_admission`), not here.
async def admit_hosted_runtime(
    pool: AsyncPool,
    agent_id: int,
    machine: str,
    owner: UUID,
    *,
    db: DatabaseLike,
) -> RuntimeIncarnation | None:
    from base.agents.recovery.breaker import admit_pending_wake

    if not await admit_pending_wake(pool, agent_id):
        return None
    recorded: list[Event] = []
    async with async_write_transaction(pool) as conn:
        claimed = await (
            await conn.execute(
                "UPDATE agents_meta m SET status='running', runtime_owner=%s, "
                "runtime_generation = CASE WHEN m.runtime_owner = %s "
                "AND m.runtime_generation IS NOT NULL "
                "THEN m.runtime_generation ELSE gen_random_uuid() END, "
                "last_admission_at = clock_timestamp(), "
                "last_launch_failure_reason = NULL, last_launch_failure_at = NULL "
                "FROM (SELECT id, status FROM agents_meta WHERE id = %s AND machine = %s "
                "AND status IN ('running','idling') FOR UPDATE) before "
                "WHERE m.id = before.id RETURNING m.runtime_generation, before.status",
                (owner, owner, agent_id, machine),
            )
        ).fetchone()
        if claimed is None:
            return None
        generation, previous_status = claimed
        incarnation = RuntimeIncarnation(agent_id, generation, owner)
        adoption, ended_lease = await _carry_impersonation(conn, agent_id, incarnation)
        if adoption is not None:
            logger.info(
                "impersonation lease carried by the replacement runtime's admission",
                agent_id=agent_id,
                lease_id=adoption.lease_id,
                session_id=adoption.session_id,
                status=adoption.status,
            )
            recorded.append(
                await record_audit_async(
                    conn,
                    prepare_event_log(
                        event_type="impersonation_lease_adopted",
                        agent_id=agent_id,
                        source="system",
                        payload=adopted_payload(
                            AdoptedLease(
                                lease_id=adoption.lease_id,
                                session_id=adoption.session_id,
                                status=adoption.status,
                                from_generation=str(adoption.accepted_from[0]),
                                from_owner=str(adoption.accepted_from[1]),
                                to_generation=str(adoption.accepted_by[0]),
                                to_owner=str(adoption.accepted_by[1]),
                                relay_provider=adoption.relay_provider,
                            )
                        ),
                    ),
                )
            )
        elif ended_lease is not None:
            logger.info(
                "impersonation lease closed by the replacement runtime's admission",
                agent_id=agent_id,
                lease_id=ended_lease,
            )
        admitted_event = prepare_event_log(
            event_type="status_change",
            agent_id=agent_id,
            source="system",
            payload=admitted_payload(previous_status),
        )
        recorded.append(await record_audit_async(conn, admitted_event))
    for event in recorded:
        emit_prepared(event)
    if ended_lease is not None:
        # meiva:note
        # This admission is what ended the lease, so this process is the host that
        # owes the external conversation its one ended-lease notice: the in-memory
        # relay child registry does not survive a restart, and there is no retry
        # ledger, so without this submission the notice is never attempted.
        # meiva:link docs/conventions/agents/agent-impersonation-hosts.md
        from base.agents.impersonation.terminal_notices import deliver_ended_notice

        await asyncio.to_thread(deliver_ended_notice, db, agent_id, ended_lease)
    logger.info("hosted runtime admitted", agent_id=agent_id, generation=str(generation))
    return incarnation


# meiva:note
# Why a turn was not admitted.
#
# `admit_hosted_runtime` answers None for four different situations, and one log line used
# to name two of them ("terminated or placed elsewhere"). The 2026-10-27 incident was the
# third — a tripped recovery breaker — and the message pointed readers at placement instead
# of at `permanent_reject_streak`, which is what had to be fixed by hand. The readings ride
# along so the caller can print the breaker's own numbers without a second query.
class AdmissionRefusal(StrEnum):
    TERMINATED = "terminated"
    PLACED_ELSEWHERE = "placed_elsewhere"
    BREAKER = "breaker"
    OTHER = "other"


@dataclass(frozen=True)
class AdmissionRefused:
    reason: AdmissionRefusal
    status: str | None = None
    streak: int | None = None
    suppress_reason: str | None = None
    suppressed_until: datetime | None = None

    # meiva:note
    # The one sentence a reader gets, and the numbers the repair needs. Each branch names its
    # own repair; the leftover branch (`other`) deliberately shares no wording with the other
    # three, so a reader can never mistake a race for a terminated row, a foreign host or a
    # tripped breaker.
    def describe(self) -> str:
        match self.reason:
            case AdmissionRefusal.TERMINATED:
                return f"row is terminated (row left {self.status})"
            case AdmissionRefusal.PLACED_ELSEWHERE:
                return f"row is hosted elsewhere (row left {self.status})"
            case AdmissionRefusal.BREAKER:
                until = self.suppressed_until.isoformat() if self.suppressed_until else "no window"
                return (
                    "the recovery breaker is tripped "
                    f"(permanent_reject_streak={self.streak}, "
                    f"wake_suppress_reason={self.suppress_reason}, "
                    f"wake_suppressed_until={until}): an operator restart or a fresh chat "
                    "admits the next turn"
                )
            case AdmissionRefusal.OTHER:
                return f"the row changed under this read (row left {self.status})"


# meiva:note
# Classify a refusal by reading the row. Callers use this only after admission returned
# None, so it costs one extra SELECT on a path that is already idle.
#
# Order is the honest one: a terminated row is terminated whatever else is on it, then a row
# owned elsewhere, then a tripped breaker. `other` is the leftover — the row changed between
# the admission read and this one, or an idling row on this machine with a clear breaker was
# refused for a reason none of these describe — and it must never borrow another reason's
# wording.
async def explain_admission_refusal(
    pool: AsyncPool, agent_id: int, machine: str
) -> AdmissionRefused:
    from base.agents.recovery.breaker import HALT_AFTER_CONSECUTIVE_PERMANENT_REJECTS

    async with pool.connection() as conn:
        row = await (
            await conn.execute(
                "SELECT status,machine,permanent_reject_streak,wake_suppress_reason,"
                "wake_suppressed_until FROM agents_meta WHERE id=%s",
                (agent_id,),
            )
        ).fetchone()
    if row is None:
        return AdmissionRefused(AdmissionRefusal.OTHER)
    status, home, streak, suppress_reason, suppressed_until = row
    if status == "terminated":
        return AdmissionRefused(AdmissionRefusal.TERMINATED, status=status)
    if home is not None and home != machine:
        return AdmissionRefused(AdmissionRefusal.PLACED_ELSEWHERE, status=status)
    if streak >= HALT_AFTER_CONSECUTIVE_PERMANENT_REJECTS:
        return AdmissionRefused(
            AdmissionRefusal.BREAKER,
            status=status,
            streak=streak,
            suppress_reason=suppress_reason,
            suppressed_until=suppressed_until,
        )
    return AdmissionRefused(AdmissionRefusal.OTHER, status=status)


# meiva:note
# Settle an ordinary turn; only durable lifecycle apply releases ownership.
#
# The corpse marker `last_turn_fatal_at` is deliberately untouched here:
# a completed LLM turn already cleared it atomically (`_persist_last_active`
# writes `last_active_at` and clears the marker in one UPDATE — the only
# `last_active_at` writer), while a no-work park (crash-fresh halted claim
# or an open circuit breaker) leaves it for the reaper. Writing it here would
# only relabel a crash-dead row healthy.
async def settle_hosted_runtime(
    pool: AsyncPool,
    incarnation: RuntimeIncarnation,
    *,
    bus: EventBus,
) -> bool:
    async with async_write_transaction(pool) as conn:
        cur = await conn.execute(
            "UPDATE agents_meta SET status = 'idling' "
            "WHERE id = %s AND status = 'running' "
            "AND runtime_generation = %s AND runtime_owner = %s",
            (
                incarnation.agent_id,
                incarnation.generation,
                incarnation.owner,
            ),
        )
        changed = cur.rowcount == 1
        settled_event = (
            await record_audit_async(
                conn,
                prepare_event_log(
                    event_type="status_change",
                    agent_id=incarnation.agent_id,
                    source="system",
                    payload=settled_payload(),
                ),
            )
            if changed
            else None
        )
    if settled_event is not None:
        emit_prepared(settled_event)
    if changed:
        await publish_agent_updated(bus, incarnation.agent_id)
    return changed


# meiva:note
# Mark this incarnation's row crash-dead after the hosted runner saw the
# turn die (fatal LLM class or an unclassified exception).
#
# COALESCE keeps the FIRST death since the last completed LLM turn: a corpse
# re-woken by heartbeats keeps re-crashing, and re-stamping each time would
# restart the reap grace forever — the reaper would never fire. Recovery
# clears the mark (a completed LLM turn, or the resurrect transition), so the
# next crash re-arms it fresh.
#
# Best-effort and settle-independent: a settlement blocked by unsettled
# resources must not lose the death evidence (the 5858 corpse settled 3m52s
# after its last turn ended; the stamp landed regardless).
async def stamp_turn_fatal(pool: AsyncPool, incarnation: RuntimeIncarnation) -> TurnFatalStamp:
    async with async_write_transaction(pool) as conn:
        cur = await conn.execute(
            "UPDATE agents_meta SET last_turn_fatal_at = "
            "COALESCE(last_turn_fatal_at, clock_timestamp()) "
            "WHERE id = %s AND status = 'running' "
            "AND runtime_generation = %s AND runtime_owner = %s RETURNING id",
            (incarnation.agent_id, incarnation.generation, incarnation.owner),
        )
        applied = await cur.fetchone() is not None
    if applied:
        logger.info(
            "hosted turn crashed — corpse marked for the reaper",
            event="host_turn_corpse_marked",
            agent_id=incarnation.agent_id,
        )
    return TurnFatalStamp(applied=applied)


# meiva:note
# Close one hosted turn: stamp the corpse marker first, then settle.
#
# The stamp is best-effort and independent of settlement (which may be
# blocked by unsettled resources): the death evidence must land even when
# the idling write could not. It must also run BEFORE the settle — its CAS
# matches `status='running'`, so a settled row would refuse the stamp.
#
# Returns both halves to the settlement boundary: the crash stamp and
# whether the settle reached idling.
async def settle_and_stamp_turn(
    pool: AsyncPool,
    incarnation: RuntimeIncarnation,
    *,
    bus: EventBus,
    exited: bool,
    crashed: bool,
) -> TurnSettlement:
    close = turn_close(exited=exited, crashed=crashed)
    stamp: TurnFatalStamp | None = None
    if close.stamp_fatal:
        try:
            stamp = await stamp_turn_fatal(pool, incarnation)
        except Exception:
            logger.opt(exception=True).warning(
                "corpse marker stamp failed — the row stays alive-"
                "looking until a later stamp or a completed turn",
                event="corpse_stamp_failed",
                agent_id=incarnation.agent_id,
            )
    settled = False
    if close.settle:
        settled = await settle_hosted_runtime(
            pool,
            incarnation,
            bus=bus,
        )
    return TurnSettlement(stamp=stamp, settled=settled)


# meiva:note
# Host start: this machine's `running` rows belong to a host that no longer exists
# (one host per machine and home), so each becomes idling. The next wake admits
# it under the new boot. This is the only place a stale row is settled.
async def settle_running_rows_at_start(pool: AsyncPool, machine: str) -> list[int]:
    async with async_write_transaction(pool) as conn:
        rows = await (
            await conn.execute(
                "UPDATE agents_meta SET status = 'idling' "
                "WHERE status = 'running' AND machine = %s RETURNING id",
                (machine,),
            )
        ).fetchall()
    settled = [row[0] for row in rows]
    if settled:
        logger.info(
            "hosted start: settled {n} running row(s) {ids}",
            event="host_stale_running_settled",
            n=len(settled),
            ids=settled,
        )
    return settled

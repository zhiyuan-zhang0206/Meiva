"""Hosted incarnation admission and settlement, replacing status-only writes."""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from agent.ownership.hosted_cleanup import finish_termination
from base import telemetry
from base.agents.impersonation.replacement import end_for_replacement
from base.agents.messages.kwargs import NoteTag
from base.agents.messages.lifecycle_text import restart_resume_note
from base.db import Database
from base.db.transaction import async_write_transaction
from base.events.live.announce import publish_agent_updated
from base.events.live.bus import EventBus
from base.log import logger
from base.native_process.runtime_incarnation import RuntimeIncarnation
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
#     agent, since one death serves them all. Returns the executed kind, or None
#     when the incarnation no longer owns the row or no command was claimed.
#
#     `resume_turn` says the restart ended a turn that was not finished (the claim
#     that took it did not find the agent idle). The same transaction then queues
#     the one pending note that continues that turn (`restart_resume_note`, source
#     the restart's own), so the restart is never done without the note that wakes
#     the agent: the pending scan finds it on this host or the next. An idle agent
#     gets no note, and neither does a terminate.
#
#     When a terminate asked to take the agent's shell sessions with it, the
#     row lock captures the next shell ID. After committing the termination,
#     `kill_shell_sessions(agent_id, cutoff)` attempts only older sessions. The
#     host reports cleanup failures; they cannot roll back the durable death.
#     A crash or a session created after this cutoff can leave live sessions for operators.
async def apply_hosted_lifecycle(
    pool: AsyncConnectionPool,
    incarnation: RuntimeIncarnation,
    *,
    bus: EventBus,
    resume_turn: bool,
    kill_shell_sessions: Callable[[int, int], None] | None = None,
) -> Literal["restart", "terminate"] | None:
    shell_cutoff: int | None = None
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
        command_id, lifecycle_kind, command_source = command
        if lifecycle_kind == "restart":
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
            if resume_turn:
                await conn.execute(
                    "INSERT INTO inbound_messages(agent_id,content,kind,source,payload) "
                    "VALUES(%s,%s,'system_note',%s,%s)",
                    (
                        incarnation.agent_id,
                        restart_resume_note(command_source),
                        command_source,
                        Jsonb({"note_tag": NoteTag.LIFECYCLE_RESTART.value}),
                    ),
                )
        elif lifecycle_kind == "terminate":
            closed = await conn.execute(
                "UPDATE inbound_messages SET status='done' WHERE agent_id=%s AND kind='terminate' "
                "AND status IN ('pending','claimed') RETURNING "
                "COALESCE((payload -> 'kill_all_shell_sessions') = 'true'::jsonb, false)",
                (incarnation.agent_id,),
            )
            if any(kill for (kill,) in await closed.fetchall()):
                if kill_shell_sessions is None:
                    raise RuntimeError("shell-session kill requested but no killer is bound")
                shell_cutoff = row[0]
            await conn.execute(
                "UPDATE agents_meta SET status='terminated',termination_source='user' WHERE id=%s",
                (incarnation.agent_id,),
            )
        else:
            raise ValueError(f"not an executable lifecycle command: {lifecycle_kind}")
    if lifecycle_kind == "terminate":
        await finish_termination(bus, incarnation.agent_id, shell_cutoff, kill_shell_sessions)
    return lifecycle_kind


# meiva:note
# Admit one turn: the circuit breaker, then one UPDATE.
#
#     Only this machine's running or idling row is claimed; a terminated or foreign
#     row yields None. The generation survives while the same host boot keeps the
#     row and is minted fresh when the owner differs, so a new boot, or a restart
#     that cleared the identity, starts a new incarnation. A host that is exiting
#     refuses new turns in process (`stop_admission`), not here.
async def admit_hosted_runtime(
    pool: AsyncConnectionPool,
    agent_id: int,
    machine: str,
    owner: UUID,
    *,
    db: Database,
) -> RuntimeIncarnation | None:
    from base.agents.recovery.breaker import admit_pending_wake

    if not await admit_pending_wake(pool, agent_id):
        return None
    recorded: list[telemetry.Event] = []
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
        ended_lease = await end_for_replacement(conn, agent_id, incarnation)
        if ended_lease is not None:
            logger.info(
                "impersonation lease closed by the replacement runtime's admission",
                agent_id=agent_id,
                lease_id=ended_lease,
            )
        admitted_event = prepare_event_log(
            event_type="status_change",
            agent_id=agent_id,
            source="system",
            payload={"from": previous_status, "to": "running"},
        )
        recorded.append(await record_audit_async(conn, admitted_event))
    for event in recorded:
        telemetry.emit_prepared(event)
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
# Settle an ordinary turn; only durable lifecycle apply releases ownership.
#
# The corpse marker `last_turn_fatal_at` is deliberately untouched here:
# a completed LLM turn already cleared it atomically (`_persist_last_active`
# writes `last_active_at` and clears the marker in one UPDATE — the only
# `last_active_at` writer), while a no-work park (crash-fresh halted claim
# or an open circuit breaker) leaves it for the reaper. Writing it here would
# only relabel a crash-dead row healthy.
async def settle_hosted_runtime(
    pool: AsyncConnectionPool,
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
                    payload={"from": "running", "to": "idling"},
                ),
            )
            if changed
            else None
        )
    if settled_event is not None:
        telemetry.emit_prepared(settled_event)
    if changed:
        await publish_agent_updated(bus, incarnation.agent_id)
    return changed


@dataclass(frozen=True)
class TurnFatalStamp:
    """One crash's stamp outcome: `applied` is the CAS result."""

    applied: bool


async def stamp_turn_fatal(
    pool: AsyncConnectionPool, incarnation: RuntimeIncarnation
) -> TurnFatalStamp:
    """Mark this incarnation's row crash-dead after the hosted runner saw the
    turn die (fatal LLM class or an unclassified exception).

    COALESCE keeps the FIRST death since the last completed LLM turn: a corpse
    re-woken by heartbeats keeps re-crashing, and re-stamping each time would
    restart the reap grace forever — the reaper would never fire. Recovery
    clears the mark (a completed LLM turn, or the resurrect transition), so the
    next crash re-arms it fresh.

    Best-effort and settle-independent: a settlement blocked by unsettled
    resources must not lose the death evidence (the 5858 corpse settled 3m52s
    after its last turn ended; the stamp landed regardless).
    """
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


@dataclass(frozen=True)
class TurnSettlement:
    """The settle boundary's facts about one closed turn: `stamp` is the crash
    stamp (None when the turn did not crash or its stamp failed), and `settled`
    says the settle wrote the row idling, gated on fully discharged turn
    resources."""

    stamp: TurnFatalStamp | None
    settled: bool


async def settle_and_stamp_turn(
    pool: AsyncConnectionPool,
    incarnation: RuntimeIncarnation,
    *,
    bus: EventBus,
    exited: bool,
    crashed: bool,
) -> TurnSettlement:
    """Close one hosted turn: stamp the corpse marker first, then settle.

    The stamp is best-effort and independent of settlement (which may be
    blocked by unsettled resources): the death evidence must land even when
    the idling write could not. It must also run BEFORE the settle — its CAS
    matches `status='running'`, so a settled row would refuse the stamp.

    Returns both halves to the settlement boundary: the crash stamp and
    whether the settle reached idling.
    """
    stamp: TurnFatalStamp | None = None
    if crashed:
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
    if not exited:
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
async def settle_running_rows_at_start(pool: AsyncConnectionPool, machine: str) -> list[int]:
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

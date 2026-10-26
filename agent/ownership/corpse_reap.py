# meiva:note
# Mark crashed hosted agents terminated and publish failure diagnostics.

from __future__ import annotations

from loguru import logger

from agent.ownership.lifecycle_plan import ReapedCorpse as ReapedCorpse
from base.db.protocols import AsyncPool
from base.deploy.progress_timeout import CORPSE_REAP_GRACE_S
from base.telemetry import Event, emit_prepared
from base.telemetry.audit_events import prepare_event_log, record_audit_async


# meiva:note
# Terminate crash-marked idling corpses whose grace window has elapsed.
#
# The positive death signal is the row's own `last_turn_fatal_at` (stamped
# firsthand by this host when the turn died), never staleness alone — an
# unmarked idling row is a live agent and is never touched. Idling-only:
# a `running` row has a live invocation (or claim park) in flight. The
# incarnation CAS already happened at stamp time, so the mark names exactly
# the generation that died; a row woken in the grace window either completes
# a turn (clears the mark) or parks and keeps it.
#
# Returns the reaped agents. The host announces them after the durable flip (the change
# hint and the browser pages), because the browser service sits above this package.
#
# Scope is this machine's rows: a host restart mints a fresh owner UUID, so
# a corpse marked under a predecessor boot is reaped like any other, and
# there is one host per machine.
async def reap_crash_corpses(
    pool: AsyncPool,
    machine: str,
) -> list[ReapedCorpse]:
    async with pool.connection() as conn, conn.transaction():
        await conn.execute("SET TRANSACTION READ WRITE")
        rows = await (
            await conn.execute(
                "UPDATE agents_meta SET status = 'terminated', "
                "termination_source = 'reaper' "
                "WHERE machine = %s AND status = 'idling' "
                "AND last_turn_fatal_at IS NOT NULL "
                "AND last_turn_fatal_at <= now() - make_interval(secs => %s) "
                "RETURNING id, EXISTS ("
                "  SELECT 1 FROM agent_impersonations owed "
                "  WHERE owed.agent_id = agents_meta.id "
                "    AND owed.status IN ('released','rejected','expired') "
                "    AND owed.handoff_applied_at IS NULL) AS handoff_unapplied",
                (machine, CORPSE_REAP_GRACE_S),
            )
        ).fetchall()
        reaped: list[ReapedCorpse] = []
        recorded: list[Event] = []
        for agent_id, handoff_unapplied in rows:
            # meiva:note
            # A reaped row that still owed a handoff receipt is a different failure from an
            # ordinary corpse: the held path settled nothing, so the reaper is the only thing
            # that noticed (#5468). The flag is read at reap time, so an operator can tell the
            # two apart without replaying lease history.
            # meiva:link docs/conventions/agents/agent-impersonation.md
            event = prepare_event_log(
                event_type="status_change",
                agent_id=agent_id,
                source="system",
                payload={
                    "from": "idling",
                    "to": "terminated",
                    "reason": "corpse_reaper",
                    "unapplied_handoff_receipt": handoff_unapplied,
                },
            )
            recorded.append(await record_audit_async(conn, event))
            reaped.append(
                ReapedCorpse(
                    agent_id,
                )
            )
    for event in recorded:
        emit_prepared(event)
    if reaped:
        logger.info(
            "corpse reaper: terminated {n} crash-dead row(s)",
            event="corpse_reaper_terminated",
            n=len(reaped),
        )
    return reaped

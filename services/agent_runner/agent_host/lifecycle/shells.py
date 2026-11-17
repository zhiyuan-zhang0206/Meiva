# meiva:note
# The lifecycle apply and the cleanup that follows a committed termination: the change hint,
# browser release and mandatory shell cleanup, which only this layer can reach.

import asyncio
from pathlib import Path

from agent.ownership.corpse_reap import ReapedCorpse, reap_crash_corpses
from agent.ownership.hosted import (
    LifecycleKind,
    apply_hosted_lifecycle,
)
from base.agents.lifecycle import release_browser_pages
from base.db.protocols import AsyncPool
from base.db.transaction import async_write_transaction
from base.events.live.announce import publish_agent_updated
from base.events.live.bus import EventBus
from base.log import logger
from base.native_process.runtime_incarnation import RuntimeIncarnation
from ops.cluster_status import ShellCleanup
from services.agent_runner.agent_host.lifecycle.cleanup_failures import (
    CleanupFailure,
    note_for,
    summarize,
)
from services.agent_runner.agent_host.lifecycle.switch_note import SwitchHistory, record_switch_note


# meiva:note
# Attempt older shell sessions after the termination transaction commits.
#
#     The mandatory at-exit shell close, run by `apply_lifecycle_command` after a
#     graceful termination commits. A failed close is logged without reopening the
#     terminal transition; the periodic reaper retries it from its retained TTL row.
def kill_terminating_agent_shells(agent_id: int, cutoff: int, *, home: Path) -> ShellCleanup | None:
    from ops.cluster_status import kill_agent_shells

    try:
        killed = kill_agent_shells(agent_id, before_session_index=cutoff, home=home)
    except Exception:
        logger.opt(exception=True).error(
            "terminate could not kill shell sessions below {cutoff} of agent {agent_id}; "
            "termination remains committed and the TTL reaper will retry",
            agent_id=agent_id,
            cutoff=cutoff,
        )
        return None
    logger.info(
        "terminate killed {count} shell session(s) below {cutoff} of agent {agent_id}: {killed}",
        agent_id=agent_id,
        cutoff=cutoff,
        count=len(killed.killed),
        killed=killed.killed,
    )
    return killed


# meiva:note
# The host removes TTL rows only after its local PTY backend made a definitive
# close/absence observation; failure leaves evidence for the remote reaper.
async def close_terminated_agent_shells(
    pool: AsyncPool, agent_id: int, cutoff: int, *, home: Path
) -> list[int] | None:
    killed = await asyncio.to_thread(kill_terminating_agent_shells, agent_id, cutoff, home=home)
    if killed is None:
        return None
    if not killed.fenced:
        return killed.killed
    async with async_write_transaction(pool) as conn:
        await conn.execute(
            "DELETE FROM agent_shell_ttls WHERE agent_id = %s AND session_id < %s",
            (agent_id, cutoff),
        )
    return killed.killed


# meiva:note
# Attempt every effect a committed termination owes, whatever an earlier one did: the change
# hint, browser release and mandatory shell cleanup below its cutoff. The first
# failure (a cancellation included) is raised after all attempts; each later one is attached to
# it as a note and logged at ERROR.
async def finish_termination(
    pool: AsyncPool, bus: EventBus, agent_id: int, shell_cutoff: int | None, *, home: Path
) -> None:
    failures: list[CleanupFailure] = []
    try:
        await publish_agent_updated(bus, agent_id)
    except BaseException as error:
        failures.append(CleanupFailure("publication", error))
    try:
        await release_browser_pages(agent_id, home=home)
    except BaseException as error:
        failures.append(CleanupFailure("browser", error))
    if shell_cutoff is not None:
        try:
            await close_terminated_agent_shells(pool, agent_id, shell_cutoff, home=home)
        except BaseException as error:
            failures.append(CleanupFailure("shell", error))
    summary = summarize(failures)
    if summary.primary is None:
        return
    for failure in summary.also_failed:
        summary.primary.add_note(note_for(failure))
        logger.opt(exception=failure.error).error(
            "termination committed but {step} cleanup also failed after an earlier failure",
            step=failure.step,
            agent_id=agent_id,
            cutoff=shell_cutoff,
        )
    raise summary.primary


# meiva:note
# Apply the claimed restart/terminate command and run what its commit owes. A terminate owes
# `finish_termination`. A restart that stored a new harness owes, afterwards, the change hint and, when `history` is given, the switch note in the history the agent left.
# Returns the applied kind, or None when the incarnation no longer owns the row or no command was
# claimed.
async def apply_lifecycle_command(
    pool: AsyncPool,
    incarnation: RuntimeIncarnation,
    *,
    bus: EventBus,
    resume_turn: bool,
    home: Path,
    history: SwitchHistory | None = None,
) -> LifecycleKind | None:
    applied = await apply_hosted_lifecycle(pool, incarnation, resume_turn=resume_turn)
    if applied is None:
        return None
    if applied.kind == "terminate":
        await finish_termination(pool, bus, applied.agent_id, applied.shell_cutoff, home=home)
    elif applied.switch is not None:
        if history is not None:
            await record_switch_note(
                history,
                applied.agent_id,
                old_harness=applied.switch.old,
                new_harness=applied.switch.new,
                source=applied.switch.source,
                message=applied.switch.message,
            )
        try:
            await publish_agent_updated(bus, applied.agent_id)
        except Exception:
            logger.opt(exception=True).warning(
                "harness switch of agent {agent_id} committed but its change hint was not "
                "published; mounted frontends refresh on their next fetch",
                agent_id=applied.agent_id,
            )
    return applied.kind


# meiva:note
# Terminate this machine's crash corpses whose grace has elapsed, then refresh mounted frontends,
# release each one's browser pages and close its shells. The durable flip already committed, so a
# failed cleanup is logged and the next corpse still gets its own cleanup attempts.
async def reap_and_announce_corpses(
    pool: AsyncPool, machine: str, *, bus: EventBus, home: Path
) -> list[ReapedCorpse]:
    reaped = await reap_crash_corpses(pool, machine)
    for corpse in reaped:
        try:
            await publish_agent_updated(bus, corpse.agent_id)
        except Exception:
            logger.exception(
                "corpse reap lifecycle hint publish failed",
                event="corpse_reaper_publish_failed",
                agent_id=corpse.agent_id,
            )
        try:
            await release_browser_pages(corpse.agent_id, home=home)
        except Exception:
            logger.exception(
                "corpse reap browser release failed",
                event="corpse_reaper_browser_release_failed",
                agent_id=corpse.agent_id,
            )
        try:
            await close_terminated_agent_shells(
                pool, corpse.agent_id, corpse.shell_cutoff, home=home
            )
        except Exception:
            logger.exception(
                "corpse reap shell cleanup failed",
                event="corpse_reaper_shell_cleanup_failed",
                agent_id=corpse.agent_id,
                cutoff=corpse.shell_cutoff,
            )
    return reaped

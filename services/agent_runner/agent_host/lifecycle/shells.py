# meiva:note
# The lifecycle apply and the cleanup that follows a committed termination: the change hint,
# the browser release and the requested shell cleanup, which only this layer can reach.

import asyncio
from pathlib import Path

from agent.ownership.corpse_reap import ReapedCorpse, reap_crash_corpses
from agent.ownership.hosted import LifecycleKind, apply_hosted_lifecycle
from base.agents.lifecycle import release_browser_pages
from base.db.protocols import AsyncPool
from base.events.live.announce import publish_agent_updated
from base.events.live.bus import EventBus
from base.log import logger
from base.native_process.runtime_incarnation import RuntimeIncarnation
from services.agent_runner.agent_host.lifecycle.cleanup_failures import (
    CleanupFailure,
    note_for,
    summarize,
)


# meiva:note
# Attempt older shell sessions after the termination transaction commits.
#
#     The at-exit half of `kill_all_shell_sessions`, run by
#     `apply_lifecycle_command` (after a graceful termination commits). Never
#     raises: a failed kill must not turn a termination into a crashed turn, so it
#     is logged at ERROR and the termination still applies.
def kill_terminating_agent_shells(agent_id: int, cutoff: int) -> None:
    from ops.cluster_status import kill_agent_shells

    try:
        killed = kill_agent_shells(agent_id, before_session_index=cutoff)
    except Exception:
        logger.opt(exception=True).error(
            "terminate could not kill shell sessions below {cutoff} of agent {agent_id}; "
            "termination remains committed",
            agent_id=agent_id,
            cutoff=cutoff,
        )
        return
    logger.info(
        "terminate killed {count} shell session(s) below {cutoff} of agent {agent_id}: {killed}",
        agent_id=agent_id,
        cutoff=cutoff,
        count=len(killed),
        killed=killed,
    )


# meiva:note
# Attempt every effect a committed termination owes, whatever an earlier one did: the change
# hint, the browser release and, when it asked, the shell cleanup below its cutoff. The first
# failure (a cancellation included) is raised after all attempts; each later one is attached to
# it as a note and logged at ERROR.
async def finish_termination(
    bus: EventBus, agent_id: int, shell_cutoff: int | None, *, home: Path
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
            await asyncio.to_thread(kill_terminating_agent_shells, agent_id, shell_cutoff)
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
# Apply the claimed restart/terminate command and run what its commit owes. A restart owes
# nothing; a terminate owes `finish_termination`. Returns the applied kind, or None when the
# incarnation no longer owns the row or no command was claimed.
async def apply_lifecycle_command(
    pool: AsyncPool,
    incarnation: RuntimeIncarnation,
    *,
    bus: EventBus,
    resume_turn: bool,
    home: Path,
) -> LifecycleKind | None:
    applied = await apply_hosted_lifecycle(pool, incarnation, resume_turn=resume_turn)
    if applied is None:
        return None
    if applied.kind == "terminate":
        await finish_termination(bus, applied.agent_id, applied.shell_cutoff, home=home)
    return applied.kind


# meiva:note
# Terminate this machine's crash corpses whose grace has elapsed, then refresh mounted frontends and
# release each one's browser pages. The durable flip already committed, so a failed hint is logged and
# the next corpse still gets its pages released.
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
        await release_browser_pages(corpse.agent_id, home=home)
    return reaped

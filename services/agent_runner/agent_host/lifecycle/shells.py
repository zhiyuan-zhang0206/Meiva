# meiva:note
# Best-effort shell cleanup that follows a committed termination.

from base.log import logger


# meiva:note
# Attempt older shell sessions after the termination transaction commits.
#
#     The at-exit half of `kill_all_shell_sessions`, bound into
#     `apply_hosted_lifecycle` (after a graceful termination commits). Never
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

# meiva:note
# The words a lifecycle command puts in an agent's history: who asked, and what a restart
# that ended an unfinished turn leaves for the agent to read. The claim node renders the
# acceptance markers from `by_who`; the host's restart apply writes `restart_resume_note`
# into the same transaction that applies the restart.
# meiva:link agent/graph/docs/turn-interrupts.meiva.okf.md


# meiva:note
# Render "by X" in lifecycle marker text. 'self' (meiva.self.terminate / restart called by the
# agent itself) becomes "yourself", which reads more naturally than "by self"; other sources
# ('user' / 'agent:N' / 'system') pass through, so the agent sees the trigger's identity.
def by_who(source: str) -> str:
    if source == "self":
        return "yourself"
    return source


# meiva:note
# The note that continues a turn a restart ended before it finished. It states only what
# the writer knows: who asked and that the turn was unfinished. What the restart did to
# the code run and the shell sessions depends on where it landed, so both are stated as
# conditions, with the way to check them.
def restart_resume_note(source: str) -> str:
    return (
        f"Restart was accepted from {by_who(source)} while your turn was unfinished. "
        "If code was running, the restart terminated it; its output so far is in the "
        "matching tool message. Your shell sessions may not have survived the restart: "
        "check `meiva.shell.sessions.list()` before relying on one."
    )

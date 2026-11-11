# meiva:note
# The words a lifecycle command puts in an agent's history: who asked, and what a restart
# that ended an unfinished turn leaves for the agent to read. The claim node renders the
# acceptance markers from `by_who`; the host's restart apply writes `restart_resume_note`
# into the same transaction that applies the restart.
# meiva:link agent/graph/docs/turn-interrupts.md


# meiva:note
# Render "by X" in lifecycle marker text. 'self' (meiva.self.terminate / restart called by the
# agent itself) becomes "yourself", which reads more naturally than "by self"; other sources
# ('user' / 'agent:N' / 'system') pass through, so the agent sees the trigger's identity.
def by_who(source: str) -> str:
    if source == "self":
        return "yourself"
    return source


# meiva:note
# The marker text recorded when an idle agent accepts a restart (or receives a passive host
# restart notice), optionally carrying the actor's natural-language explanation.
def restart_accepted_text(source: str, message: str | None = None) -> str:
    head = f"Restart was accepted from {by_who(source)}"
    if message:
        return f"{head}: {message}"
    return head


# meiva:note
# The note an interrupt writes ahead of its message: who interrupted the turn, and whether a message
# from them follows. It states the fact only; what the agent does next is its own decision.
def interrupt_note(source: str, *, with_message: bool) -> str:
    head = f"Your running turn was interrupted by {by_who(source)}."
    return f"{head} Their message follows." if with_message else head


# meiva:note
# The note that continues a turn a restart ended before it finished. It states only what
# the writer knows: who asked, why (when explained), and that the turn was unfinished. What
# the restart did to the code run and the shell sessions depends on where it landed, so both
# are stated as conditions, with the way to check them.
def restart_resume_note(source: str, message: str | None = None) -> str:
    tail = (
        "If code was running, the restart terminated it; its output so far is in the "
        "matching tool message. Your shell sessions may not have survived the restart: "
        "check `meiva.shell.sessions.list()` before relying on one."
    )
    if message:
        return (
            f"Restart was accepted from {by_who(source)} while your turn was unfinished: "
            f"{message}\n{tail}"
        )
    return f"Restart was accepted from {by_who(source)} while your turn was unfinished. {tail}"


# meiva:note
# The note that starts the first turn after a restart that carried a transition. A switch says
# which harness the agent left and which it now runs on (the format every harness switch uses, so
# the agent reads one shape whoever asked); a restart that kept its harness is the plain acceptance
# text, because nothing was terminated mid-turn: the turn that asked for it ended first.
def restart_transition_note(
    source: str, message: str | None, *, from_harness: str, to_harness: str
) -> str:
    if from_harness != to_harness:
        return f"[Harness switched from '{from_harness}' to '{to_harness}']\n{message or ''}"
    return restart_accepted_text(source, message)

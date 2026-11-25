# meiva:note
# The words a lifecycle command puts in an agent's history: who asked, and what a restart
# that ended an unfinished turn leaves for the agent to read. The claim node renders the
# acceptance markers from `by_who`; the host's restart apply writes `restart_resume_note`
# into the same transaction that applies the restart.
# meiva:link agent/graph/docs/turn-interrupts.md


from base.host.boundary.json_values import is_object_dict


def _fast_acknowledgement_overlay_value(config: object) -> bool | None:
    if not is_object_dict(config):
        return None
    overlay = config.get("config_overlay")
    target = overlay if is_object_dict(overlay) else config
    raw = target.get("fast_acknowledgement")
    return raw if isinstance(raw, bool) else None


# meiva:note
# System-note update injected when a restart or transition explicitly sets fast_acknowledgement.
def fast_acknowledgement_note(config: object) -> str | None:
    enabled = _fast_acknowledgement_overlay_value(config)
    if enabled is None:
        return None
    if enabled:
        return (
            "[Config updated: fast_acknowledgement=on] On direct, unambiguous user requests, "
            "emit one brief `MEIVA_DISPLAY_TO_USER` confirmation before calling any tool, then "
            "execute silently under `MEIVA_HIDE_FROM_USER` (stay silent after pure-action "
            "commands that need no report)."
        )
    return (
        "[Config updated: fast_acknowledgement=off] Do not send empty or low-information "
        "acknowledgements ('Got it, I'll...', 'On it', 'Delegated, will report back') before "
        "calling tools or mid-task; work silently under `MEIVA_HIDE_FROM_USER` until delivering "
        "the final result or hitting a real question or blocker."
    )


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
    return f"{head}: {message}" if message else head


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
# The one text of a harness switch, whoever asked and wherever it is read: the harness left, the
# harness now running, who asked, that nothing said before the switch is visible to the new
# harness, and the restart message, which is the whole handoff.
# meiva:link base/agents/epochs/docs/epochs.md
def harness_switch_note(
    source: str,
    message: str | None,
    *,
    from_harness: str,
    to_harness: str,
) -> str:
    head = (
        f"[Harness switched from '{from_harness}' to '{to_harness}' by {by_who(source)}]\n"
        "Nothing said before the switch is visible here; this message is the handoff."
    )
    return f"{head}\n\n{message}" if message else head

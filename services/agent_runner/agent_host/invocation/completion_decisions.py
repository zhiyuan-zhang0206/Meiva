# meiva:note
# What a completed graph invocation asks the host to do next, as pure functions of the result the
# graph returned and the lifecycle command kind the apply committed. `finish_completed_invocation`
# executes the plan these answer.

from __future__ import annotations

from collections.abc import Mapping


# meiva:note
# Whether a completed graph invocation requested a hosted lifecycle apply (restart or terminate).
def lifecycle_return_requested(result: Mapping[str, object]) -> bool:
    return bool(result["exit_requested"] or result["restart_requested"])


# meiva:note
# After a restart claim `halted` is the claim's verdict on the agent it found: set, it was
# idle; clear, its turn was unfinished and the apply queues the note that continues it.
# meiva:link agent/graph/docs/turn-interrupts.meiva.okf.md
def restart_resumes_turn(result: Mapping[str, object]) -> bool:
    return bool(result["restart_requested"]) and not bool(result["halted"])


# meiva:note
# Whether a committed lifecycle command queued a continuation note that this host's scheduler
# should wake immediately.
# meiva:link services/agent_runner/agent_host/lifecycle/docs/graceful-exit.meiva.okf.md
def should_wake_resumed(kind: str | None, *, resume_turn: bool) -> bool:
    return kind == "restart" and resume_turn

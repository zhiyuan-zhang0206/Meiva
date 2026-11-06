# meiva:note
# The one thing a schedule-launched process may not do with the identity it carries: drive the agent
# that identity belongs to. A schedule is a script in this process, and it reaches the database, the
# filesystem and the shell like any other, so a list of permitted SDK entries would neither describe
# nor bound what it can do -- `schedules/` already calls tasks, shell, web and
# `agents.spawn/terminate/resurrect`. What it must not do is end, restart, compact, resurrect
# or cancel the loop it speaks for: that is how a timer seizes the agent it was handed.
#
# The actor string is the evidence. The runner binds `schedule:<id>` with the owner as the agent id,
# and a script cannot mint one (`establish_actor` does not exist, and the source scan in
# `tests/ci/test_schedule_identity.py` keeps it that way).
#
# It lives in its own module rather than in `agent_identity.py` because that module holds no process
# global (`tests/ci/test_leak_guard.py` reads that as `__annotations__ == {}`), and a rule with a
# table of actions is a fact of its own, not another line of the identity door.
#
# This is not a sandbox. A schedule reads and writes the database, the filesystem and the shell
# through the same context as the agent it speaks for; the refusal narrows one action, not what the
# process can reach.
from __future__ import annotations

from meiva.sdk_surface import agent_identity

_SCHEDULE_ACTOR_PREFIX = "schedule:"

_STEERING_ACTIONS: frozenset[str] = frozenset(
    {
        "agents.cancel",
        "agents.compact",
        "agents.restart",
        "agents.resurrect",
        "agents.terminate",
    }
)


# meiva:note
# Refuse a `schedule:<id>` process that steers the agent it speaks for, and raise `RuntimeError`
# when it aims `action` at that agent. It may still speak and dispatch -- send a message, open a
# card, spawn and end workers -- because a scheduled batch legitimately does all of that.
def assert_schedule_capability(action: str, *, target_agent_id: int | None) -> None:
    caller = agent_identity.bound_caller()
    if caller is None or not (caller.actor or "").startswith(_SCHEDULE_ACTOR_PREFIX):
        return
    if action not in _STEERING_ACTIONS or target_agent_id is None:
        return
    if target_agent_id != caller.agent_id:
        return
    raise RuntimeError(
        f"meiva.{action}({target_agent_id}) is refused here: this process was launched by a "
        f"schedule and carries agent {target_agent_id} as its identity (`schedule:<id>` with that "
        f"owner bound). Ending, restarting, compacting, resurrecting or cancelling the "
        f"loop it speaks for would let a timer seize the agent it was handed. Drive a worker "
        f"instead, or do this from the agent itself. This is not a sandbox: the script still holds "
        f"the database, the filesystem and the shell of the process it runs in."
    )

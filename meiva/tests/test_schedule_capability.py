# meiva:note
# A schedule speaks for an agent; it does not drive the one it speaks for.
#
# A schedule-launched process carries its owner as `agent_id` and `schedule:<id>` as its actor, so
# every SDK entry is reachable from an unattended script. What it must never aim at that owner is a
# lifecycle action: ending, restarting, compacting or cancelling the loop it was handed would let a
# timer seize the agent whose identity it borrowed. Everything else stays available, because a
# scheduled batch legitimately sends messages, spawns workers and reads the cluster.
#
# The refusal runs before the gateway call, so a refused target never reaches the network, and the
# source scan below fails if any of the four entries loses its guard.

from __future__ import annotations

import ast
from collections.abc import Callable
from functools import partial
from pathlib import Path

import pytest

import meiva
from meiva.agents import schedule_capability
from meiva.tests.fixtures.pin_agent import pin_agent, pin_no_identity

_REPO_ROOT = Path(__file__).resolve().parents[2]
_AGENTS = _REPO_ROOT / "meiva/agents/__init__.py"
_OWNER = 405
_SCHEDULE_ACTOR = "schedule:55"
_STEERING_ACTIONS = (
    "agents.cancel",
    "agents.compact",
    "agents.restart",
    "agents.resurrect",
    "agents.retry_launch",
    "agents.terminate",
)

# meiva:note
# The entries that steer a loop, and the one argument each is aimed with. Written out here rather
# than imported so a renamed entry or a dropped guard fails this test instead of shrinking the rule.
_STEERING: tuple[tuple[str, Callable[[int], object]], ...] = (
    ("agents.cancel", meiva.agents.cancel),
    ("agents.compact", meiva.agents.compact),
    ("agents.restart", partial(meiva.agents.restart, message="why")),
    ("agents.resurrect", partial(meiva.agents.resurrect, prompt="wake")),
    (
        "agents.retry_launch",
        partial(meiva.agents.retry_launch, idempotency_key="k", expected_prior_attempt_id="a"),
    ),
    ("agents.terminate", meiva.agents.terminate),
)

# meiva:note
# Entries that write to an agent on purpose: speaking to it is the schedule's job, whoever the
# target is. Every other public entry that both names a target `agent_id` and mutates it must carry
# the guard -- that is what makes a new lifecycle entry fail here instead of shipping unguarded.
_SPEAKING = frozenset({"send_message"})

# meiva:note
# What an entry may call on `gateway_client` without changing the target's runtime state. Anything
# else it calls there is a mutation, and a mutation aimed by `agent_id` needs the guard.
_READS = frozenset(
    {
        "get_agent",
        "get_ancestors",
        "get_born_chain",
        "get_last_message",
        "get_launch_attempt",
        "get_neighbors",
        "get_status",
        "harness_availability",
        "list_agents",
        "list_machines",
        "list_open_pages",
    }
)


def test_a_schedule_may_not_steer_the_agent_it_speaks_for() -> None:
    pin_agent(_OWNER, owns_loop=False, actor=_SCHEDULE_ACTOR)
    for action in _STEERING_ACTIONS:
        with pytest.raises(
            RuntimeError, match="let a timer seize the agent it was handed"
        ) as refused:
            schedule_capability.assert_schedule_capability(action, target_agent_id=_OWNER)
        assert action in str(refused.value)


def test_a_schedule_may_drive_a_worker() -> None:
    pin_agent(_OWNER, owns_loop=False, actor=_SCHEDULE_ACTOR)
    for action in _STEERING_ACTIONS:
        schedule_capability.assert_schedule_capability(action, target_agent_id=999)


def test_an_agent_actor_is_never_refused_by_this_rule() -> None:
    pin_agent(_OWNER, owns_loop=True, actor="agent:405")
    for action in _STEERING_ACTIONS:
        schedule_capability.assert_schedule_capability(action, target_agent_id=_OWNER)


def test_a_process_without_an_identity_is_never_refused() -> None:
    pin_no_identity()
    schedule_capability.assert_schedule_capability("agents.terminate", target_agent_id=_OWNER)


# meiva:note
# The guard is wired at each entry, not only in the plan: a schedule process calling one of the four
# against its owner gets the refusal before the gateway is touched. Without the guard the call would
# fail differently (a transport or argument error), which is what makes this a test of the wiring.
@pytest.mark.parametrize(("action", "call"), _STEERING, ids=[name for name, _ in _STEERING])
def test_a_schedule_process_is_refused_at_the_entry(
    action: str, call: Callable[[int], object]
) -> None:
    pin_agent(_OWNER, owns_loop=False, actor=_SCHEDULE_ACTOR)
    with pytest.raises(RuntimeError, match="is refused here") as refused:
        _ = call(_OWNER)
    assert action in str(refused.value)


# meiva:note
# The expectation set the rule needs. Naming today's six entries would only protect today's six, so
# the scan asks the question that survives a new entry: does each public `meiva.agents` function
# that names a target `agent_id` and mutates that agent through the gateway carry the guard? An
# entry that speaks (`send_message`) is exempt by design; anything else that mutates without the
# guard fails here, which is the "a new lifecycle entry cannot ship unguarded" half of the rule.
def _calls_guard(node: ast.FunctionDef) -> bool:
    for call in (n for n in ast.walk(node) if isinstance(n, ast.Call)):
        func = call.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name == "assert_schedule_capability":
            return True
    return False


# meiva:note
# The `gateway_client.<name>` calls in this entry that change an agent's runtime state: everything it
# calls there that is not in `_READS`.
def _gateway_mutations(node: ast.FunctionDef) -> set[str]:
    found: set[str] = set()
    for call in (n for n in ast.walk(node) if isinstance(n, ast.Call)):
        func = call.func
        if not isinstance(func, ast.Attribute) or not isinstance(func.value, ast.Name):
            continue
        if func.value.id == "gateway_client" and func.attr not in _READS:
            found.add(func.attr)
    return found


# meiva:note
# The module's declared agent-visible functions (`__all_for_meiva__`), not every non-underscore name:
# `send_system_note` is a real helper there but not part of the surface an agent is offered.
def _surface_entries() -> dict[str, ast.FunctionDef]:
    tree = ast.parse(_AGENTS.read_text(encoding="utf-8"), filename=str(_AGENTS))
    surface: set[str] = set()
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
        if "__all_for_meiva__" not in targets or not isinstance(node.value, ast.List):
            continue
        surface = {
            elt.value
            for elt in node.value.elts
            if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
        }
    assert surface, "meiva/agents/__init__.py lost its __all_for_meiva__ surface"
    return {
        node.name: node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in surface
    }


def test_every_entry_that_mutates_a_named_agent_carries_the_guard() -> None:
    unguarded: list[str] = []
    for name, node in sorted(_surface_entries().items()):
        args = [arg.arg for arg in node.args.posonlyargs + node.args.args]
        if "agent_id" not in args or name in _SPEAKING:
            continue
        mutations = _gateway_mutations(node)
        if mutations and not _calls_guard(node):
            unguarded.append(f"{name} ({', '.join(sorted(mutations))})")
    assert unguarded == [], (
        "these entries change a named agent's runtime state without the schedule guard: "
        + "; ".join(unguarded)
    )


def test_the_six_steering_entries_are_the_ones_that_carry_the_guard() -> None:
    guarded = {
        f"agents.{name}"
        for name, node in _surface_entries().items()
        if not name.startswith("_") and _calls_guard(node)
    }
    assert guarded == set(_STEERING_ACTIONS), (
        "the guarded entries and the steering list disagree: guarded="
        f"{sorted(guarded)} steering={sorted(_STEERING_ACTIONS)}"
    )

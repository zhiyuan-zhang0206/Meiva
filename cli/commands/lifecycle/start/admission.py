# meiva:note
# Whether this start may go on, and what it must ask of the cluster before launching.
#
# Every function here is a decision over values the start shell already read: the home's
# destroy intent and checkout rule, the roles the unit resolved against the roles `meiva init`
# recorded, and the roles that decide which cluster services a start touches.

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass

__all__ = [
    "Refusal",
    "capability_refusal",
    "guard_refusal",
    "probes_gateway",
    "roles_line",
]


# meiva:note
# A start that ends before any effect; `message` goes to stderr and the start exits 1.
@dataclass(frozen=True)
class Refusal:
    message: str


# meiva:note
# The refusal of a home the start must not operate, or None.
#
# Raises:
#     RuntimeError: the home is being destroyed or detached. This is no refusal to
#         report: nothing may run against it.
def guard_refusal(*, destroy_intent: bool, checkout_error: str | None) -> Refusal | None:
    if destroy_intent:
        raise RuntimeError("home is being destroyed or detached; startup refused")
    if checkout_error:
        return Refusal(f"✗ {checkout_error}")
    return None


# meiva:note
# The refusal when the roles this unit resolved differ from the ones `meiva init` recorded.
#
# The capability set is fixed when a home is initialized. A home with no recorded intent
# has nothing to differ from.
def capability_refusal(roles: Collection[str], recorded: Collection[str] | None) -> Refusal | None:
    if recorded is None or sorted(roles) == sorted(recorded):
        return None
    return Refusal(
        f"\n✗ this home's capabilities ({','.join(sorted(roles))}) differ from the ones "
        f"`meiva init` recorded ({','.join(recorded)}); capabilities are fixed when "
        "a home is initialized"
    )


# meiva:note
# The line that names the identity this start runs as.
def roles_line(roles: Collection[str], machine_name: str) -> str:
    return f"\n→ roles = {','.join(sorted(roles))}, machine = {machine_name}"


# meiva:note
# Whether the start probes the gateway over the private network before launching.
#
# A pure agent-runner reaches its gateway for self-heal updates and cluster status, so a
# broken path should fail on this start's stdout. A box that serves the gateway too is the
# gateway: it has nothing to dial.
def probes_gateway(roles: Collection[str]) -> bool:
    return "agent-runner" in roles and "gateway" not in roles

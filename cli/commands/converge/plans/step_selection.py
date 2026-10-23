# meiva:note
# Which converge steps run on this host, and why each other step does not.
#
# A pure decision over the steps' scopes and the facts the composition point read once: the
# unit's capabilities (`None` until the unit is configured), the services this operation
# selected and whether the home is the host's prod install. The shell in
# `cli/commands/lifecycle/converge_host.py` prints the decisions and runs the steps.

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["RunStep", "SkipStep", "StepDecision", "StepScope", "plan_steps"]


# meiva:note
# What one step is scoped to: the facts its selection depends on, not its work.
@dataclass(frozen=True)
class StepScope:
    name: str
    roles: frozenset[str]
    requires_unit_config: bool
    host_global: bool
    services: frozenset[str]


@dataclass(frozen=True)
class RunStep:
    name: str


@dataclass(frozen=True)
class SkipStep:
    name: str
    reason: str


StepDecision = RunStep | SkipStep


# meiva:note
# One decision per scope, in order.
#
# A step needing a consumer service runs only when one is selected; host-global wiring runs
# only for the host's prod install; a configured unit runs the steps scoped to a capability
# it carries; a unit that is not configured yet skips the steps that need its configuration.
def plan_steps(
    scopes: tuple[StepScope, ...],
    *,
    roles: frozenset[str] | None,
    services: frozenset[str],
    prod_install: bool,
) -> tuple[StepDecision, ...]:
    return tuple(
        _decide(scope, roles=roles, services=services, prod_install=prod_install)
        for scope in scopes
    )


def _decide(
    scope: StepScope,
    *,
    roles: frozenset[str] | None,
    services: frozenset[str],
    prod_install: bool,
) -> StepDecision:
    if scope.services and not scope.services.intersection(services):
        return SkipStep(scope.name, "consumer service not selected")
    if scope.host_global and not prod_install:
        return SkipStep(
            scope.name, "dev cluster/worktree — host-global wiring is prod-install only"
        )
    if roles is not None and not (roles & scope.roles):
        return SkipStep(scope.name, f"roles {','.join(sorted(roles))}")
    if roles is None and scope.requires_unit_config:
        return SkipStep(scope.name, "unit not configured yet; initialize with meiva start")
    return RunStep(scope.name)

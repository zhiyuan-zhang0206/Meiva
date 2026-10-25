# meiva:note
# Fixed reader fault for consumers that must propagate invalid capability tokens.
# meiva:link base/cluster/machine/docs/machine.meiva.okf.md

from __future__ import annotations

from base.cluster.machine import (
    MachineFacts,
    MachineRoleInvalid,
    MachineRoles,
)

__all__ = ["InvalidRoleIdentity"]


class InvalidRoleIdentity:
    def __init__(self, facts: MachineFacts) -> None:
        self._facts = facts

    def name(self) -> str:
        return self._facts.name()

    def role(self) -> MachineRoles:
        raise MachineRoleInvalid("machine_role invalid; fixed test fault.")

    def host(self) -> str:
        return self._facts.host()

    def description(self) -> str | None:
        return self._facts.description()

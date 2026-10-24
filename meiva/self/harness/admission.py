from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from meiva.self.harness.registration import HarnessRegistration, RegistrationError

__all__ = [
    "ProcessLineage",
    "descends",
    "require_claimed",
    "require_live_registration",
]


@dataclass(frozen=True, slots=True)
class ProcessLineage:
    pid: int
    ancestors: frozenset[int]


def descends(controller_pid: int, lineage: ProcessLineage) -> bool:
    if controller_pid <= 0:
        return False
    return lineage.pid == controller_pid or controller_pid in lineage.ancestors


# meiva:note
# Admission is three questions about one row, asked again on every SDK call: is the
# session still attached, has it expired, and does this process run where and under
# the controller the row names. They are a pure function of the row, the machine, the
# process lineage and the instant, so the decision is testable without booting a
# process, and a row that stops qualifying stops the caller at its next action.
# meiva:link meiva/self/harness/docs/harness-attach.meiva.okf.md
def require_live_registration(
    registration: HarnessRegistration,
    *,
    machine: str,
    lineage: ProcessLineage,
    now: datetime,
) -> HarnessRegistration:
    if not registration.attached:
        raise RegistrationError(
            f"harness session {registration.external_session_id} is {registration.status}"
        )
    if registration.expired(now):
        raise RegistrationError(
            f"harness session {registration.external_session_id} expired at "
            f"{registration.expires_at}"
        )
    if registration.machine != machine:
        raise RegistrationError(
            f"the harness session belongs to machine {registration.machine!r}; "
            f"this process runs on {machine!r}"
        )
    if not descends(registration.controller_pid, lineage):
        raise RegistrationError(
            "this process does not descend from the controller that started the "
            f"harness session (pid {registration.controller_pid})"
        )
    return registration


def require_claimed(
    registration: HarnessRegistration | None, *, agent_id: int | None, session_id: str | None
) -> HarnessRegistration:
    if registration is None:
        raise RegistrationError("no harness session matches this handle")
    if agent_id is not None and registration.agent_id != agent_id:
        raise RegistrationError(
            f"this handle belongs to agent {registration.agent_id}, not {agent_id}"
        )
    if session_id is not None and registration.external_session_id != session_id:
        raise RegistrationError("this handle belongs to another external session")
    return registration

# meiva:note
# The stored lease row read as a decision: the phase it names, whether it still owes its handoff,
# and the relay view of it. Pure readers of one `agent_impersonations` row, here because the
# decisions they feed (`gate`, `relay`) live in this package; the module that also writes the row
# (`agent/impersonation.py`) re-exports the two names its callers import from it.

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime

from agent.takeover.gate import LeasePhase, handoff_due
from agent.takeover.relay import LeaseView
from base.agents.impersonation.status import ImpersonationStatus, PublicLease
from base.agents.observation.relay_supervision import heartbeat_fresh


# meiva:note
# The phase a stored lease row names. The column keeps the persisted status (a string), so the match
# accepts both the enum and its value; a status this build does not know stops the read.
def lease_phase(session: Mapping[str, object] | None) -> LeasePhase | None:
    if session is None:
        return None
    match session["status"]:
        case ImpersonationStatus.REQUESTED:
            return LeasePhase.REQUESTED
        case ImpersonationStatus.ACCEPTED:
            return LeasePhase.ACCEPTED
        case ImpersonationStatus.ACTIVE:
            return LeasePhase.ACTIVE
        case (
            ImpersonationStatus.RELEASED
            | ImpersonationStatus.REJECTED
            | ImpersonationStatus.EXPIRED
        ):
            return LeasePhase.ENDED
        case unknown:
            raise ValueError(f"unknown impersonation lease status: {unknown!r}")


# meiva:note
# Whether the row still owes its handoff: an ended lease whose `handoff_applied_at` never landed.
def handoff_owed(session: Mapping[str, object] | None) -> bool:
    phase = lease_phase(session)
    return (
        session is not None
        and phase is not None
        and handoff_due(phase, receipt_written=session["handoff_applied_at"] is not None)
    )


# meiva:note
# The relay view of a stored lease row: its phase, how fresh its heartbeat is, and the age of the
# token it was minted with. None for a row whose status names no phase.
def lease_view(session: PublicLease, *, now: datetime) -> LeaseView | None:
    phase = lease_phase(session)
    if phase is None:
        return None
    minted_at = session.get("relay_minted_at")
    return LeaseView(
        lease_id=session["id"],
        generation=session["relay_generation"],
        phase=phase,
        heartbeat_fresh=heartbeat_fresh(session["relay_heartbeat_at"], now=now),
        minted_age_s=None if minted_at is None else (now - minted_at).total_seconds(),
    )

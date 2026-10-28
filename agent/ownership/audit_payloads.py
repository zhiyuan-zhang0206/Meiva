from __future__ import annotations

from dataclasses import dataclass


# meiva:note
# The audit payload of a `status_change` the ownership layer records, by what happened: the host
# admitted a turn on a row that was `previous_status`, a turn closed and the row settled to idling,
# or the reaper terminated a crash-dead row (`handoff_unapplied` says it still owed a handoff
# receipt, so an operator can tell it from an ordinary corpse without replaying lease history).
def admitted_payload(previous_status: str) -> dict[str, object]:
    return {"from": previous_status, "to": "running"}


# meiva:note
# The audit payload of `impersonation_lease_adopted`: the takeover a replacement native
# incarnation carried instead of ending. `from_*` is the accepting pair the lease named
# before the move, `to_*` the admitted incarnation that carries it now; `status` is what
# the lease was at the move (accepted or active). Together with the lease's own
# `lease_adopted` log entry this is the only record of who accepted it.
@dataclass(frozen=True)
class AdoptedLease:
    # meiva:note
    # One carried takeover as the fleet reads it: the lease and its agent-scoped handle, the
    # status at the move, the accepting pair it moved from and to, and the relay provider.
    lease_id: str
    session_id: int
    status: str
    from_generation: str
    from_owner: str
    to_generation: str
    to_owner: str
    relay_provider: str | None


def adopted_payload(lease: AdoptedLease) -> dict[str, object]:
    return {
        "lease_id": lease.lease_id,
        "session_id": lease.session_id,
        "status": lease.status,
        "from_generation": lease.from_generation,
        "from_owner": lease.from_owner,
        "to_generation": lease.to_generation,
        "to_owner": lease.to_owner,
        "relay_provider": lease.relay_provider,
    }


def settled_payload() -> dict[str, object]:
    return {"from": "running", "to": "idling"}


def reaped_payload(*, handoff_unapplied: bool) -> dict[str, object]:
    return {
        "from": "idling",
        "to": "terminated",
        "reason": "corpse_reaper",
        "unapplied_handoff_receipt": handoff_unapplied,
    }

from __future__ import annotations


# meiva:note
# The audit payload of a `status_change` the ownership layer records, by what happened: the host
# admitted a turn on a row that was `previous_status`, a turn closed and the row settled to idling,
# or the reaper terminated a crash-dead row (`handoff_unapplied` says it still owed a handoff
# receipt, so an operator can tell it from an ordinary corpse without replaying lease history).
def admitted_payload(previous_status: str) -> dict[str, object]:
    return {"from": previous_status, "to": "running"}


def settled_payload() -> dict[str, object]:
    return {"from": "running", "to": "idling"}


def reaped_payload(*, handoff_unapplied: bool) -> dict[str, object]:
    return {
        "from": "idling",
        "to": "terminated",
        "reason": "corpse_reaper",
        "unapplied_handoff_receipt": handoff_unapplied,
    }

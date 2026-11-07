from __future__ import annotations


# meiva:note
# The audit payload of a `status_change` the ownership layer records, by what happened: the host
# admitted a turn on a row that was `previous_status`, a turn closed and the row settled to idling,
# or the reaper terminated a crash-dead row.
def admitted_payload(previous_status: str) -> dict[str, object]:
    return {"from": previous_status, "to": "running"}


def settled_payload() -> dict[str, object]:
    return {"from": "running", "to": "idling"}


def reaped_payload() -> dict[str, object]:
    return {
        "from": "idling",
        "to": "terminated",
        "reason": "corpse_reaper",
    }

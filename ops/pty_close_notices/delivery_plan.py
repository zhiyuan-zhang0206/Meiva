# meiva:note
# Which of a batch's notices are delivered, as values.
#
# A notice this attempt does not own (an earlier attempt claimed its key: delivered then,
# dropped then, or left behind when that attempt died after committing) is skipped, so a re-sent
# notice is never delivered twice. Of the owned ones, only an owner that is still alive receives
# its notice; a terminated or unknown owner's notice is dropped without delivery and without
# resurrecting anyone (the TTL reaper's boundary, gateway/ttl_reaper/__init__.py).

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from ops.pty_close_notices.notice import ClosureNotice, claim_key

__all__ = ["DeliveryPlan", "Dropped", "plan_delivery"]

_NOTIFIABLE_STATUSES = ("running", "idling")


# meiva:note
# An owned notice that is not delivered, with the owner's status at the time (None when the
# agent is unknown).
@dataclass(frozen=True)
class Dropped:
    notice: ClosureNotice
    status: str | None


# meiva:note
# The notices to insert as inbound messages, and the owned ones dropped instead, in batch order.
@dataclass(frozen=True)
class DeliveryPlan:
    deliver: tuple[ClosureNotice, ...]
    dropped: tuple[Dropped, ...]


# meiva:note
# Decide from the keys this attempt claimed and every owner's status.
def plan_delivery(
    batch: Sequence[ClosureNotice], *, owned: frozenset[str], statuses: Mapping[int, str | None]
) -> DeliveryPlan:
    deliver: list[ClosureNotice] = []
    dropped: list[Dropped] = []
    for notice in batch:
        if claim_key(notice) not in owned:
            continue
        status = statuses.get(notice.agent_id)
        if status in _NOTIFIABLE_STATUSES:
            deliver.append(notice)
        else:
            dropped.append(Dropped(notice, status))
    return DeliveryPlan(tuple(deliver), tuple(dropped))

# meiva:note
# What a native receiver decides for one immutable upload batch, as values.
#
# The shell (`ops/upload_delivery/__init__.py`) reads the database, the disk and the
# gateway, and executes what these functions return. Nothing here reads a clock, a path,
# a connection or the network: the same facts always give the same plan, so the quota and
# idempotency rules are tested without a database.

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

__all__ = [
    "Admission",
    "FetchObject",
    "ObjectStep",
    "ReuseObject",
    "admission",
    "fetch_timeout_s",
    "plan_objects",
]


# meiva:note
# How a copy request stands against the reservation row the receiver may already hold.
#
# `NEW` reserves the batch, `RESUME` re-admits a batch this exact request and native root
# already reserved (a ready marker is historical, so the quota is checked again), and
# `CONFLICT` means the batch id already binds another manifest or another native root.
class Admission(Enum):
    NEW = "new"
    RESUME = "resume"
    CONFLICT = "conflict"


# meiva:note
# Decide the admission of a copy request from the three facts the shell read.
#
# `same_request` and `same_directory` compare the stored row with this request; they are
# meaningless when `row_exists` is false, and a request that claims otherwise is a caller bug.
def admission(*, row_exists: bool, same_request: bool, same_directory: bool) -> Admission:
    if not row_exists:
        if same_request or same_directory:
            raise ValueError("a missing reservation row cannot match this request")
        return Admission.NEW
    if same_request and same_directory:
        return Admission.RESUME
    return Admission.CONFLICT


# meiva:note
# The object is already a file on the native root: verify it and read it, never fetch it again.
@dataclass(frozen=True)
class ReuseObject:
    ordinal: int


# meiva:note
# The object is absent: pull it from the gateway at `path`, a path under the gateway API base.
@dataclass(frozen=True)
class FetchObject:
    ordinal: int
    path: str


ObjectStep = ReuseObject | FetchObject


# meiva:note
# One step per manifest object, in manifest order: reuse a file that exists, fetch one that
# does not. `present[i]` says whether object `i` exists on the native root.
def plan_objects(
    present: tuple[bool, ...], *, agent_id: int, batch_id: str
) -> tuple[ObjectStep, ...]:
    steps: list[ObjectStep] = []
    for ordinal, exists in enumerate(present):
        if exists:
            steps.append(ReuseObject(ordinal))
        else:
            steps.append(
                FetchObject(
                    ordinal,
                    f"/api/keyed/v1/agents/{agent_id}/uploads/{batch_id}/objects/{ordinal}",
                )
            )
    return tuple(steps)


# meiva:note
# The timeout of the next gateway request, or None when the batch's bounded network round is
# spent. One request never outlives `request_cap_s` nor the time that is left.
def fetch_timeout_s(*, remaining_s: float, request_cap_s: float) -> float | None:
    if request_cap_s <= 0:
        raise ValueError("a gateway request needs a positive timeout cap")
    if remaining_s <= 0:
        return None
    return min(request_cap_s, remaining_s)

# meiva:note
# How a failed copy of a delivered-upload batch is recorded: values in, a verdict out.
#
# `gateway.upload_delivery.worker` owns the effects (the copy, the receiver dial, the state
# write). Whether a failure can ever succeed on retry, and the reason it records, are decided
# here from the failure alone.

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

__all__ = ["CopyFailure", "receiver_failure", "terminal_failure"]

_TERMINAL_RECEIVER_REASONS = frozenset(
    {
        "upload-copy-conflict-v1",
        "upload-copy-quota-v1",
        "upload-source-unavailable-v1",
    }
)


# meiva:note
# The recorded reason of a failed copy and whether the batch is held (never retried).
@dataclass(frozen=True)
class CopyFailure:
    reason: str
    hold: bool


# meiva:note
# The verdict for a receiver that answered 'failed': a runner that does not know the
# receive op or one that refused for a reason that cannot change holds the batch; anything
# else is retried.
def receiver_failure(result: Mapping[str, object]) -> CopyFailure:
    if str(result.get("error", "")).startswith("unknown kind:"):
        return CopyFailure("unsupported-receiver", hold=True)
    reason = result.get("reason")
    if reason in _TERMINAL_RECEIVER_REASONS:
        return CopyFailure(str(reason), hold=True)
    return CopyFailure("receiver-unavailable", hold=False)


# meiva:note
# The verdict for a failure that never succeeds on retry; a conflict names itself.
def terminal_failure(error_name: str, detail: str, *, conflict: bool) -> CopyFailure:
    return CopyFailure(detail if conflict else error_name, hold=True)

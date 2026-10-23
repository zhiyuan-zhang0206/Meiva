# meiva:note
# Whether a stop retires the extras, and what it needs for that to be possible.

from __future__ import annotations

__all__ = ["retained_refusal", "retires_helper"]


# meiva:note
# Why the extras cannot be retired while `retained` units stay up, or None.
#
# Self-defence for the action itself: retiring the permissions helper needs the service root
# gone, which any retained unit prevents. The flag-level refusal lives in
# `plan.require_consistent`, where the operator's own choices can still be named; here only
# the fact of retention matters.
def retained_refusal(retained: frozenset[str]) -> str | None:
    if not retained:
        return None
    return (
        "retiring extras needs the service root stopped, but this stop retains "
        f"{', '.join(sorted(retained))}; stop them too, or keep extras"
    )


# meiva:note
# The permissions helper exists on macOS only; elsewhere there is nothing to retire.
def retires_helper(system: str) -> bool:
    return system == "darwin"

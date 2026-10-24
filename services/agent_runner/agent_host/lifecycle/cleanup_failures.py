# meiva:note
# Which failure of a committed termination's cleanup is raised and which only annotate it.
#
# Every post-commit effect is attempted whatever an earlier one did, so the failures arrive in
# attempt order. The first one is the failure of the termination (a cancellation included);
# each later one is a note on it. The shell attaches the notes and logs them at ERROR.
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

__all__ = ["CleanupFailure", "FailureSummary", "note_for", "summarize"]


# meiva:note
# One failed post-commit effect: its name (`publication`, `browser`, `shell`) and what it raised.
@dataclass(frozen=True)
class CleanupFailure:
    step: str
    error: BaseException


# meiva:note
# The failure to raise (None when every effect succeeded) and the later failures it is annotated with.
@dataclass(frozen=True)
class FailureSummary:
    primary: BaseException | None
    also_failed: tuple[CleanupFailure, ...]


def summarize(failures: Sequence[CleanupFailure]) -> FailureSummary:
    if not failures:
        return FailureSummary(None, ())
    return FailureSummary(failures[0].error, tuple(failures[1:]))


def note_for(failure: CleanupFailure) -> str:
    return f"Post-commit {failure.step} cleanup also failed: {failure.error!r}"

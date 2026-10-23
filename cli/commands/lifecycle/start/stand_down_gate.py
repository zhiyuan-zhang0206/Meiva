# meiva:note
# What a start does about the home's stand-down record.
#
# A stop leaves a record that the OS boot job obeys (`stand_down.py` owns the record). The
# boot job's start leaves a stopped home alone while the record holds; an explicit start is
# the one thing that ends a stand-down, so it clears the record and says so. The shell reads
# the record once and executes the step this module returns.

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["StandDownStep", "stand_down_step"]


# meiva:note
# What the start does first: the line to report, whether to delete the record, and
# whether the start ends here with success (the boot job leaves a stopped home down).
@dataclass(frozen=True)
class StandDownStep:
    line: str | None
    clear: bool
    skip: bool


# meiva:note
# The step for a start that found `recorded` stand-down state in its home.
#
# `window` is the record's own description of its deadline and `deadline_passed` whether
# that deadline is behind the start; both are facts of the record, read by the shell.
def stand_down_step(
    *, boot_attempt: bool, recorded: bool, deadline_passed: bool, window: str
) -> StandDownStep:
    if boot_attempt:
        if not recorded or deadline_passed:
            return StandDownStep(line=None, clear=False, skip=False)
        return StandDownStep(
            line=f"stand-down ({window}): the boot job leaves this home stopped",
            clear=False,
            skip=True,
        )
    if recorded:
        return StandDownStep(line="stand-down cleared by an explicit start", clear=True, skip=False)
    return StandDownStep(line=None, clear=False, skip=False)

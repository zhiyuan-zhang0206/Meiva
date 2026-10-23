# meiva:note
# Who wins when a stall verdict and the end of the script meet: values in, verdicts out.
#
# The stall guard runs on its own thread while the script finishes on the main one. Closing
# the guard and claiming a stall are two transitions of one small state; applying them in
# either order must leave exactly one winner. ``engine.StallGuard`` holds the state under a
# lock and applies these transitions, so the interleaving is a property of two pure functions.
# Frame recognition (what counts as a deliberate park) and the choice of which failure of a
# guard close survives are decided here too.

from __future__ import annotations

from dataclasses import dataclass

from services.wake.schedule_manager.script_outcome import exit_code_of

__all__ = [
    "Admission",
    "Frame",
    "ParkFiles",
    "admit_stall",
    "close_failure_is_fatal",
    "is_parked",
    "stop_admission",
]

# meiva:note
# Frames that legitimately park the main thread for unbounded time: a resident schedule's
# whole reason for existing is a long sleep between fire windows. The DEEPEST frame decides.
_PARK_FRAME_NAMES = ("sleep", "wait", "wait_for", "run_forever", "acquire")


# meiva:note
# Whether the guard was closed and whether it claimed a stall.
@dataclass(frozen=True)
class Admission:
    stopped: bool = False
    stalled: bool = False


# meiva:note
# The facts of one sampled frame and of its caller.
@dataclass(frozen=True)
class Frame:
    filename: str
    function: str
    parent_filename: str | None
    parent_function: str | None


# meiva:note
# Source file identities of the two stdlib waits that park a child wait.
@dataclass(frozen=True)
class ParkFiles:
    subprocess: str
    selectors: str


# meiva:note
# Claim the stall unless the guard was already closed.
def admit_stall(state: Admission) -> tuple[Admission, bool]:
    if state.stopped:
        return state, False
    return Admission(stopped=state.stopped, stalled=True), True


# meiva:note
# Close the guard; the second element says whether a stall action was admitted first.
def stop_admission(state: Admission) -> tuple[Admission, bool]:
    return Admission(stopped=True, stalled=state.stalled), state.stalled


# meiva:note
# Whether the sampled frame is a deliberate park rather than work.
#
# Sleep-family frames and the actual subprocess wait park; so does the selector wait
# immediately inside ``subprocess``'s ``_communicate``. Spawn, argument conversion, stdin
# flush and selectors anywhere else stay guarded.
def is_parked(frame: Frame, files: ParkFiles) -> bool:
    if frame.function in _PARK_FRAME_NAMES:
        return True
    if (frame.filename, frame.function) == (files.subprocess, "_wait"):
        return True
    if (frame.filename, frame.function) != (files.selectors, "select"):
        return False
    return (frame.parent_filename, frame.parent_function) == (files.subprocess, "_communicate")


# meiva:note
# Whether a guard close failure is raised rather than logged.
#
# A script that failed keeps its own failure as the primary one; a script that ended
# cleanly (no exception, or ``SystemExit`` with a success code) is not allowed to complete
# over a guard that failed to stop.
def close_failure_is_fatal(primary: BaseException | None) -> bool:
    if primary is None:
        return True
    return isinstance(primary, SystemExit) and exit_code_of(primary.code) == 0

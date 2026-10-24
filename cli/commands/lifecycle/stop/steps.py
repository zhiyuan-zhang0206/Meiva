# meiva:note
# The steps of a stop, as values.
#
# A stop plan (`plan.plan_stop`) is a tuple of these in the order they run; the shell
# (`execute`) performs them one at a time. A step carries exactly what the shell needs that the
# plan decided: which units to preserve, whether the stop interrupts, what a data-plane skip says.

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import assert_never

from cli.commands.lifecycle.stop.choices import StopWindow

__all__ = [
    "Announce",
    "ClearServing",
    "CloseShellSessions",
    "DataPlane",
    "ReapChrome",
    "RecordBrowserSession",
    "SkipDataPlane",
    "StopBrowser",
    "StopDataPlane",
    "StopExtras",
    "StopPlan",
    "StopScope",
    "StopServices",
    "StopStep",
    "phase_label",
]


# meiva:note
# What this stop does with the cluster's own Postgres and Redis.
class DataPlane(Enum):
    # meiva:note
    # this host has no local data plane (no gateway capability)
    NONE = "none"
    KEPT = "kept"
    STOPPED = "stopped"


# meiva:note
# Revoke the prior start's recovery authority before any daemon sees its own shutdown.
@dataclass(frozen=True)
class ClearServing: ...


# meiva:note
# Record the stand-down and tell the gateway this unit is stopping on purpose.
@dataclass(frozen=True)
class Announce:
    window: StopWindow


# meiva:note
# Write the session ledger a later start restores, before the browser is signalled.
@dataclass(frozen=True)
class RecordBrowserSession: ...


# meiva:note
# Stop the root-owned units not in `preserve`; `phase` names the timed phase.
@dataclass(frozen=True)
class StopServices:
    preserve: frozenset[str]
    force: bool
    phase: str


# meiva:note
# Wait for this cluster's Chrome to exit, without escalating a signal.
@dataclass(frozen=True)
class StopBrowser: ...


# meiva:note
# Kill any Chrome still running on this cluster's profile (force only).
@dataclass(frozen=True)
class ReapChrome: ...


# meiva:note
# Close the persistent shell sessions through the pty-sessions service.
@dataclass(frozen=True)
class CloseShellSessions:
    direct_db: bool
    force: bool


# meiva:note
# Retire the extras (the permissions helper); only legal while nothing is retained.
@dataclass(frozen=True)
class StopExtras:
    retained: frozenset[str]


# meiva:note
# Stop this cluster's own Postgres and Redis (data preserved on disk).
@dataclass(frozen=True)
class StopDataPlane:
    force: bool


# meiva:note
# Say that the data plane is left alone (a force stop narrates its skips).
@dataclass(frozen=True)
class SkipDataPlane:
    plane: DataPlane


StopStep = (
    ClearServing
    | Announce
    | RecordBrowserSession
    | StopServices
    | StopBrowser
    | ReapChrome
    | CloseShellSessions
    | StopExtras
    | StopDataPlane
    | SkipDataPlane
)


# meiva:note
# What the confirm gate shows. `tree_preserve` is the unit set a force stop lists the
# service tree against (None: the stop lists no tree).
@dataclass(frozen=True)
class StopScope:
    force: bool
    shells_kept: bool
    browser_kept: bool
    data_plane: DataPlane
    tree_preserve: frozenset[str] | None


# meiva:note
# The steps a stop runs, in order, and the scope its confirm gate shows.
@dataclass(frozen=True)
class StopPlan:
    steps: tuple[StopStep, ...]
    scope: StopScope


# meiva:note
# The timed phase a normal stop records `step` under, or None for an untimed step.
def phase_label(step: StopStep) -> str | None:
    match step:
        case RecordBrowserSession():
            return "browser-session"
        case StopServices(_, _, phase):
            return phase
        case StopBrowser():
            return "browser"
        case CloseShellSessions():
            return "shell-sessions"
        case StopExtras():
            return "extras"
        case StopDataPlane():
            return "data-plane"
        case ClearServing() | Announce() | ReapChrome() | SkipDataPlane():
            return None
        case _:
            assert_never(step)

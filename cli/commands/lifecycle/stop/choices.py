# meiva:note
# What an operator chose for one stop, and the host facts a stop is planned from.
#
# Every keep-or-stop choice is explicit: nothing here defaults or infers one. The shell reads
# the host facts once (`StopHost`) and hands them to `plan.plan_stop` with the choices; the plan
# decides everything else.

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "BROWSER_SESSION",
    "StopChoices",
    "StopHost",
    "StopRequest",
    "StopRoster",
    "StopWindow",
]

# meiva:note
# The browser service runs a headed Chrome on a persistent login profile; the operator keeps
# or stops it explicitly (`--keep-browser`/`--stop-browser`).
BROWSER_SESSION = "browser"


# meiva:note
# The operator's answers.
#
# `keep_infra` is None exactly on a host without the gateway capability (no local data
# plane); every other choice is a required explicit answer. `announce` says whether this
# stop tells the gateway and records a stand-down (a restart's stop leg does neither).
@dataclass(frozen=True)
class StopChoices:
    keep_infra: bool | None
    preserve_sessions: frozenset[str]
    keep_browser: bool
    keep_shell_sessions: bool
    announce: bool
    teardown_extras: bool
    force: bool


# meiva:note
# The maintenance window a stop declares; both None for an ordinary stop.
@dataclass(frozen=True)
class StopWindow:
    for_seconds: int | None
    reason: str | None


# meiva:note
# One stop as the command line states it: the choices, the window, the confirm gate and
# the budget. `timeout` bounds the whole stop; nothing here defaults or infers one.
@dataclass(frozen=True)
class StopRequest:
    choices: StopChoices
    window: StopWindow
    require_confirmation: bool
    timeout: float


# meiva:note
# The service roster as a plan needs it: every unit, and the ones that need the database.
@dataclass(frozen=True)
class StopRoster:
    known: frozenset[str]
    requires_db: frozenset[str]


# meiva:note
# What the shell read from this host before planning.
#
# `roster` is None when it was not read: an explicit force stop reads none, so it stays usable
# when the settings the roster derives its ports from cannot be loaded, and the plan then
# cannot check the kept services against it. `pty_unit` is the roster unit of the pty-sessions
# service, and `unstarted` is the positive first-start evidence that no application could
# have admitted work (always False for a force stop, which never asks).
@dataclass(frozen=True)
class StopHost:
    roles: frozenset[str]
    roster: StopRoster | None
    pty_unit: str
    unstarted: bool

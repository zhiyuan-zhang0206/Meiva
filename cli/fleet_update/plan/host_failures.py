# meiva:note
# How a window reports a host that did not come up: the step it failed at and why, one line per
# listed host in the matrix, and the exit that tells automation a partial up from a refusal.
# meiva:link cli/docs/fleet-update-window/fleet-update-window.md

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TypeGuard

from ..untyped import object_of

# meiva:note
# Exit code 2 already means "refused before doing anything"; a run where some hosts came up and
# others did not is its own outcome, so automation can tell a partial up from a rejected one.
PARTIAL_FAILURE_EXIT = 3


def _is_object_dict(value: object) -> TypeGuard[dict[str, object]]:
    return isinstance(value, dict)


# meiva:note
# Why a runner's start was not tried: runners join through the gateway, so with the gateway down
# the attempt could only fail on the connection.
NOT_ATTEMPTED_WITHOUT_GATEWAY = (
    "not attempted: the gateway did not start, and runners join through it"
)


@dataclass(frozen=True)
class HostFailure:
    alias: str
    step: str
    reason: str

    def line(self) -> str:
        return f"{self.alias} failed at {self.step!r}: {self.reason}"


# meiva:note
# How far two machines' clocks may disagree before a marker's own stamp stops being evidence
# about *this* attempt: the marker is stamped on the host, the window is measured here. Two
# minutes covers an NTP-synced fleet, and a host drifting further loses its attribution rather
# than lending it to the wrong step.
_CLOCK_SKEW = timedelta(seconds=120)


# meiva:note
# The step and reason a host's converge recorded when it aborted, else the start's own exit.
#
# The marker outlives the wave that wrote it — only a fully successful converge on that home
# clears it — so `window` is what makes it evidence: the record counts only when its own `at`
# falls inside the window this attempt occupied, widened by `_CLOCK_SKEW`. A marker from an
# earlier wave is dropped and the failure is reported at the start with the start's own words,
# never with an older step's: a wrong attribution sends the reader to the wrong machine.
def start_failure(
    alias: str,
    incomplete: str | None,
    exit_reason: str,
    *,
    window: tuple[datetime, datetime],
) -> HostFailure:
    try:
        recorded = object_of(json.loads, incomplete or "")
        if not _is_object_dict(recorded):
            raise TypeError("incomplete converge record must be a mapping")
        step = str(recorded["step"])
        error = str(recorded["error"])
        at = datetime.fromisoformat(str(recorded["at"]))
    except (KeyError, TypeError, ValueError):
        return HostFailure(alias, "start", exit_reason)
    if not _within(at, window):
        return HostFailure(alias, "start", exit_reason)
    return HostFailure(alias, step, error)


# meiva:note
# Whether a marker's moment sits inside the attempt's window, clocks allowed to disagree. A
# marker or window without a zone cannot be placed on that line at all, so it is not evidence.
def _within(at: datetime, window: tuple[datetime, datetime]) -> bool:
    started, finished = window
    try:
        return started - _CLOCK_SKEW <= at <= finished + _CLOCK_SKEW
    except TypeError:
        return False


# meiva:note
# One line per listed host, whatever the others did: serving, the step it failed at, or why the
# window could not verify it at all.
def outcome(alias: str, failures: tuple[HostFailure, ...], skipped: Mapping[str, str]) -> str:
    failed = [failure for failure in failures if failure.alias == alias]
    if failed:
        return "failed at " + "; ".join(f"{failure.step!r}: {failure.reason}" for failure in failed)
    if alias in skipped:
        return f"skipped: {skipped[alias]}"
    return "serving"


RESULT_PREFIX = "fleet-update-result: "


def _entry(failure: HostFailure) -> dict[str, str]:
    return {"host": failure.alias, "step": failure.step, "reason": failure.reason}


def result_line(deferred: tuple[HostFailure, ...], failures: tuple[HostFailure, ...]) -> str:
    record = {
        "status": "partial",
        "deferred": [_entry(failure) for failure in deferred],
        "failed": [_entry(failure) for failure in failures],
    }
    return RESULT_PREFIX + json.dumps(record, sort_keys=True)


def partial_summary(
    deferred: tuple[HostFailure, ...], failures: tuple[HostFailure, ...], *, gateway: str
) -> tuple[str, ...]:
    if not deferred and not failures:
        return ()
    lines = [
        *(
            f"DEFERRED: {failure.alias} left out at {failure.step!r}: {failure.reason}"
            for failure in deferred
        ),
        *(f"PARTIAL: {failure.line()}" for failure in failures),
    ]
    if failures:
        lines.append(
            f"up incomplete: {len(failures)} host(s) failed; the rest are serving. "
            "Fix each and rerun `up` (idempotent)."
        )
    if any(failure.alias == gateway for failure in failures):
        lines.append(
            "the gateway did not come up: runners were not started, and the window does not start "
            "the old version again, because the new gateway start may already have migrated the "
            "database. Fix the gateway, then rerun `up`."
        )
    if deferred:
        names = ", ".join(failure.alias for failure in deferred)
        lines.append(
            f"deferred: {names} {'was' if len(deferred) == 1 else 'were'} left out of this update "
            "and not started on the new commit; the healthy hosts went on without "
            f"{'it' if len(deferred) == 1 else 'them'}. Catch each up with a later `meiva update run` "
            "once reachable; to run `up` after a split `down`, name only the serving runners."
        )
    lines.append(result_line(deferred, failures))
    return tuple(lines)

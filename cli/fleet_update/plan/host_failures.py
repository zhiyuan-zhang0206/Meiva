# meiva:note
# How a window reports a host that did not come up: the step it failed at and why, one line per
# listed host in the matrix, and the exit that tells automation a partial up from a refusal.
# meiva:link cli/docs/fleet-update-window/fleet-update-window.meiva.okf.md

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import cast

# meiva:note
# Exit code 2 already means "refused before doing anything"; a run where some hosts came up and
# others did not is its own outcome, so automation can tell a partial up from a rejected one.
PARTIAL_FAILURE_EXIT = 3


@dataclass(frozen=True)
class HostFailure:
    alias: str
    step: str
    reason: str

    def line(self) -> str:
        return f"{self.alias} failed at {self.step!r}: {self.reason}"


# meiva:note
# The step and reason a host's converge recorded when it aborted, else the start's own exit. A
# host whose converge aborted leaves the rest of the cluster running: the report names its step.
def start_failure(alias: str, incomplete: str | None, exit_reason: str) -> HostFailure:
    try:
        recorded = cast("dict[str, object]", json.loads(incomplete or ""))
        return HostFailure(alias, str(recorded["step"]), str(recorded["error"]))
    except (KeyError, TypeError, ValueError):
        return HostFailure(alias, "start", exit_reason)


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


def incomplete_summary(failures: tuple[HostFailure, ...]) -> tuple[str, ...]:
    return (
        *(f"PARTIAL: {failure.line()}" for failure in failures),
        f"up incomplete: {len(failures)} host(s) failed; the rest are serving. "
        "Fix each and rerun `up` (idempotent).",
    )

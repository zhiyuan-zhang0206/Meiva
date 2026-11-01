# meiva:note
# The up half of a window: start the gateway and then each runner with the operator's explicit
# start selection, prove the cluster, and end the window's silence. One host's converge failure
# must not stop the rest: each start is attempted, the failures are collected with their step and
# reason, and the caller reports them and exits 3. A half-up cluster is reported, never hidden
# behind a single abort that leaves the remaining hosts untouched.
# meiva:link cli/docs/fleet-update-window/fleet-update-window.meiva.okf.md

from __future__ import annotations

import argparse
from datetime import datetime, timedelta

from .choices import start_flags
from .fleet_verify import verify
from .oneshot import start_script
from .plan.host_commands import INCOMPLETE
from .plan.host_failures import HostFailure, outcome, start_failure
from .plan.wave_order import start_order
from .programs import VERIFY_SCRIPTS, program_text, run_silence
from .session import FailedError, Session


# meiva:note
# What a failed start is attributed to. The converge marker counts only as evidence about *this*
# run: `window` opens when the window started and closes after the longest attempt its own
# `--start-timeout` allows, so a marker from an earlier wave falls outside it and the failure is
# reported at the start instead. The clock is the value the entry point read, never one this
# module takes itself.
def _host_failure(
    s: Session, alias: str, exc: FailedError, window: tuple[datetime, datetime]
) -> HostFailure:
    try:
        recorded: str | None = s.run(alias, INCOMPLETE, effect=False)
    except FailedError:
        recorded = None
    return start_failure(alias, recorded, str(exc), window=window)


def _start_one(
    s: Session,
    args: argparse.Namespace,
    alias: str,
    window: tuple[datetime, datetime],
) -> HostFailure | None:
    script = start_script(
        macos=s.os[alias] == "Darwin",
        alias=alias,
        stamp=s.stamp,
        timeout_s=args.start_timeout,
        flags=start_flags(args),
    )
    try:
        s.run(alias, script)
    except FailedError as exc:
        return _host_failure(s, alias, exc, window)
    return None


# meiva:link cli/docs/fleet-update-window/fleet-update-window.meiva.okf.md
def up(s: Session, args: argparse.Namespace) -> list[HostFailure]:
    # meiva:note
    # Start the gateway first and then all runners in parallel, then verify every host: one
    # failure never erases what the window learned about the others, and each listed host ends
    # with its own line.
    failures: list[HostFailure] = []
    window = (s.now, s.now + timedelta(seconds=args.start_timeout))
    hosts = start_order(args.gateway, tuple(args.runner))
    started: set[str] = set()
    gateway_failure = _start_one(s, args, args.gateway, window)
    if gateway_failure is not None:
        failures.append(gateway_failure)
        s.say(f"PARTIAL: {gateway_failure.line()}")
    else:
        started.add(args.gateway)
    runners = tuple(args.runner)
    runner_outcomes = s.run_all(runners, lambda alias: _start_one(s, args, alias, window))
    for alias, failure in zip(runners, runner_outcomes, strict=True):
        if failure is not None:
            failures.append(failure)
            s.say(f"PARTIAL: {failure.line()}")
        else:
            started.add(alias)
    verified, skipped, commit = verify(
        s,
        args,
        hosts=hosts,
        started=frozenset(started),
        gateway_stdin=program_text(VERIFY_SCRIPTS / "fleet_gateway.py"),
    )
    failures += verified
    # meiva:note
    # The planned-outage silence ends here even on a partial up: hosts that came back healthy
    # must not spend the rest of the silence with their alerts suppressed.
    run_silence(s, args.gateway, "close")
    for alias in hosts:
        s.say(f"matrix: {alias} {outcome(alias, tuple(failures), skipped)}")
    if not failures:
        s.say(f"up complete: every listed machine runs {commit[:12]}")
    return failures

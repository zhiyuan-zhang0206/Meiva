# meiva:note
# The up half of a window: start the gateway and then each runner with the operator's explicit
# start selection, prove the cluster, and end the window's silence. One host's converge failure
# must not stop the rest: each start is attempted, the failures are collected with their step and
# reason, and the caller reports them and exits 3. A half-up cluster is reported, never hidden
# behind a single abort that leaves the remaining hosts untouched. The one dependency is the
# gateway: a runner joins the cluster through it, so when it did not start the runners are not
# attempted (each would only fail on the unreachable gateway) and the matrix says so.
# meiva:link cli/docs/fleet-update-window/fleet-update-window.meiva.okf.md

from __future__ import annotations

from datetime import datetime, timedelta

from .choices import FleetArgs, start_flags
from .fleet_verify import verify
from .oneshot import start_script
from .plan.host_commands import INCOMPLETE
from .plan.host_failures import (
    NOT_ATTEMPTED_WITHOUT_GATEWAY,
    HostFailure,
    outcome,
    start_failure,
)
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
    args: FleetArgs,
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
def up(s: Session, args: FleetArgs) -> list[HostFailure]:
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
    if gateway_failure is not None:
        runner_outcomes = [
            HostFailure(alias, "start", NOT_ATTEMPTED_WITHOUT_GATEWAY) for alias in runners
        ]
    else:
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
    # The planned-outage silence is shortened to its post-`up` settle grace here even on a
    # partial up: hosts that came back healthy must not spend the rest of a multi-hour silence
    # with their alerts suppressed.
    run_silence(s, args.gateway, "close")
    for alias in hosts:
        s.say(f"matrix: {alias} {outcome(alias, tuple(failures), skipped)}")
    if not failures:
        s.say(f"up complete: every listed machine runs {commit[:12]}")
    return failures

# meiva:note
# The window's verification stage, on the hosts: which machine each host is, the roster until
# every listed machine agrees on one commit, a smoke test per serving agent-runner, the package
# refresh and the drift checks. What each answer means is decided in `plan/`; this reads the
# answers and records one line per host. Like the rest of `cli.fleet_update`, nothing here
# imports application code.
# meiva:link cli/docs/fleet-update-window/fleet-update-window.meiva.okf.md

from __future__ import annotations

import argparse
import time

from .plan.gateway_commands import (
    drift_command,
    machine_name_command,
    refresh_command,
    roster_command,
    smoke_command,
)
from .plan.host_failures import HostFailure
from .plan.roster import (
    RosterRow,
    missing_rows,
    parse_roster,
    poll_delay,
    snapshot,
    timeout_message,
    unlisted_lines,
)
from .plan.verify_steps import drift_checks, duplicate_names, machine_name, smoke_plan
from .session import FailedError, Session


def machine_names(s: Session, aliases: tuple[str, ...]) -> dict[str, str]:
    names: dict[str, str] = {}
    for alias in aliases:
        reported = machine_name(s.run(alias, machine_name_command(), effect=False))
        if reported is None:
            raise FailedError(f"{alias}: the host printed no machine name")
        names[alias] = reported
    if problem := duplicate_names(names):
        raise FailedError(problem)
    return names


# meiva:note
# Read the roster until every listed machine is online and agrees on one commit. A host this run
# already knows failed to start is expected to be offline: waiting the whole roster window for it
# would turn one host's failure into everybody's timeout.
# meiva:link cli/docs/fleet-update.meiva.okf.md
def roster(
    s: Session,
    args: argparse.Namespace,
    names: dict[str, str],
    gateway_stdin: str,
    *,
    expect_offline: frozenset[str],
) -> tuple[tuple[RosterRow, ...], str]:
    wanted = frozenset(names.values())
    started = time.monotonic()
    while True:
        out = s.run(args.gateway, roster_command(), effect=False, stdin=gateway_stdin)
        try:
            rows = parse_roster(out)
        except ValueError as exc:
            raise FailedError(str(exc)) from exc
        if missing := missing_rows(rows, wanted):
            raise FailedError(f"no roster row for {list(missing)} (host aliases to names: {names})")
        seen = snapshot(rows, wanted, expect_offline)
        if seen.settled and seen.commit is not None:
            for line in unlisted_lines(rows, wanted):
                s.say(line)
            return seen.listed, seen.commit
        delay = poll_delay(time.monotonic() - started, args.roster_timeout, s.pacing.roster_poll_s)
        if delay is None:
            for line in unlisted_lines(rows, wanted):
                s.say(line)
            raise FailedError(timeout_message(args.roster_timeout, seen))
        time.sleep(delay)


def refresh(s: Session, hosts: tuple[str, ...]) -> list[HostFailure]:
    # meiva:note
    # One host's package refresh failing is that host's line in the report, not the end of the
    # verification: the others are still refreshed and still checked.
    failures: list[HostFailure] = []
    for alias in hosts:
        try:
            out = s.run(alias, refresh_command())
        except FailedError as exc:
            failures.append(HostFailure(alias, "packages refresh", str(exc)))
            continue
        if out:
            s.say(f"packages refresh [{alias}] {out.splitlines()[-1].strip()}")
    return failures


def run_drift_checks(s: Session, gateway: str, hosts: tuple[str, ...]) -> list[HostFailure]:
    failures: list[HostFailure] = []
    for check in drift_checks(gateway, hosts):
        try:
            s.run(check.alias, drift_command(check.verb), effect=True)
        except FailedError as exc:
            failures.append(HostFailure(check.alias, check.verb, str(exc)))
    return failures


def verify(
    s: Session,
    args: argparse.Namespace,
    *,
    hosts: tuple[str, ...],
    started: frozenset[str],
    gateway_stdin: str,
) -> tuple[list[HostFailure], dict[str, str], str]:
    # meiva:note
    # Roster, then one smoke per serving machine, then refresh and drift -- each host's answer
    # recorded on its own line. A host the window already knows failed to start is expected to
    # be offline, so one failure never costs the rest the whole roster timeout.
    failures: list[HostFailure] = []
    skipped: dict[str, str] = {}
    commit = ""
    if args.dry_run:
        s.run(args.gateway, smoke_command("MACHINE", args.smoke_timeout) + "  # each agent-runner")
        skipped.update(dict.fromkeys(hosts, "dry run"))
    else:
        try:
            names = machine_names(s, hosts)
            failed_names = frozenset(names[a] for a in hosts if a not in started)
            rows, commit = roster(s, args, names, gateway_stdin, expect_offline=failed_names)
        except FailedError as exc:
            failures += [HostFailure(alias, "roster", str(exc)) for alias in hosts]
        else:
            for step in smoke_plan(rows, names, started):
                if not step.runs:
                    skipped.setdefault(step.owner, "start failed")
                    continue
                try:
                    smoke = smoke_command(step.machine, args.smoke_timeout)
                    s.run(args.gateway, smoke, stdin=gateway_stdin)
                except FailedError as exc:
                    failures.append(HostFailure(step.owner, "smoke", str(exc)))
    failures += refresh(s, hosts)
    failures += run_drift_checks(s, args.gateway, hosts)
    return failures, skipped, commit

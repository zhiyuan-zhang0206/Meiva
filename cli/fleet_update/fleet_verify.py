# meiva:note
# The window's verification stage: which host is serving, which failed at what, and which could
# not be verified at all. It lives beside `cli.fleet_update` because that module is at its file
# ceiling; like the gate, this one imports no application code.
# meiva:link cli/docs/fleet-update-window/fleet-update-window.meiva.okf.md

from __future__ import annotations

import argparse
import json
import shlex
import time
from dataclasses import dataclass
from typing import Any

from .fleet_gate import Runner

POLL_S = 5


# meiva:note
# A window step that cannot complete. This module owns the class so the half that starts hosts
# and the half that verifies them raise and catch the same one.
class FailedError(RuntimeError):
    pass


HOME = 'H="$HOME/.meiva"; S="$H/source"; cd "$S" || exit 1'
MEIVA = 'MEIVA_HOME="$H" "$S/.venv/bin/meiva"'
_PYTHON = f'{HOME}; MEIVA_HOME="$H" MEIVA_CONFIG_FETCH=skip "$S/.venv/bin/python"'


@dataclass(frozen=True)
class HostFailure:
    alias: str
    step: str
    reason: str

    def line(self) -> str:
        return f"{self.alias} failed at {self.step!r}: {self.reason}"


def machine_names(s: Runner, aliases: list[str]) -> dict[str, str]:
    ask = f'{_PYTHON} -c "from base.config import ConfigBoot; from base.cluster.machine.boot import machine_from_boot; print(machine_from_boot(ConfigBoot()).name())"'
    names: dict[str, str] = {}
    for alias in aliases:
        reported = s.run(alias, ask, effect=False).split()
        if not reported:
            raise FailedError(f"{alias}: the host printed no machine name")
        names[alias] = reported[-1]
    if len(set(names.values())) != len(names):
        raise FailedError(f"two listed hosts report one machine name: {names}")
    return names


# meiva:link cli/docs/fleet-update.meiva.okf.md
def _window_state(
    rows: list[dict[str, Any]], wanted: set[str], expect_offline: frozenset[str]
) -> tuple[list[dict[str, Any]], list[str], set[tuple[str, str | None]], str | None]:
    # meiva:note
    # One roster read as the three facts the wait needs: the listed rows, which of them are
    # offline (a host this run already failed is expected to be), and the commits they report.
    listed = [row for row in rows if row["name"] in wanted]
    offline = [
        row["name"]
        for row in listed
        if (not row["online"] or row["identity_mismatch"]) and row["name"] not in expect_offline
    ]
    committed = [row for row in listed if row["name"] not in expect_offline]
    commits = {(row["head_sha"], row["running_sha"]) for row in committed}
    return listed, offline, commits, (committed or listed)[0]["head_sha"]


def roster(
    s: Runner,
    args: argparse.Namespace,
    names: dict[str, str],
    program: str,
    gateway_stdin: str,
    *,
    clock: Any = time,
    expect_offline: frozenset[str] = frozenset(),
) -> tuple[list[dict[str, Any]], str]:
    # meiva:note
    # A host this run already knows failed to start is expected to be offline: waiting the whole
    # roster window for it would turn one host's failure into everybody's timeout.
    wanted = set(names.values())
    deadline = clock.monotonic() + args.roster_timeout
    while True:
        out = s.run(args.gateway, f"{program} roster", effect=False, stdin=gateway_stdin)
        rows = json.loads(out.splitlines()[-1])
        if missing := sorted(wanted - {row["name"] for row in rows}):
            raise FailedError(f"no roster row for {missing} (host aliases to names: {names})")
        listed, offline, commits, commit = _window_state(rows, wanted, expect_offline)
        if not offline and commit is not None and commits == {(commit, commit)}:
            _report_unlisted(s, rows, wanted)
            return listed, commit
        if clock.monotonic() >= deadline:
            _report_unlisted(s, rows, wanted)
            raise FailedError(
                f"roster after {args.roster_timeout}s: offline {offline}; commits {commits}"
            )
        clock.sleep(POLL_S)


def _report_unlisted(s: Runner, rows: list[dict[str, Any]], wanted: set[str]) -> None:
    for row in rows:
        if row["name"] not in wanted:
            s.say(
                f"roster: {row['name']} is not listed, not checked: online={row['online']} "
                f"head={row['head_sha']} running={row['running_sha']}"
            )


def refresh(s: Runner, args: argparse.Namespace) -> list[HostFailure]:
    # meiva:note
    # One host's package refresh failing is that host's line in the report, not the end of the
    # verification: the others are still refreshed and still checked.
    failures: list[HostFailure] = []
    for alias in [args.gateway, *args.runner]:
        try:
            out = s.run(alias, f"{HOME}; {MEIVA} packages refresh")
        except FailedError as exc:
            failures.append(HostFailure(alias, "packages refresh", str(exc)))
            continue
        if out:
            s.say(f"packages refresh [{alias}] {out.splitlines()[-1].strip()}")
    return failures


def drift_checks(s: Runner, args: argparse.Namespace) -> list[HostFailure]:
    checks = [(args.gateway, "schedules verify --no-notify")]
    checks += [(alias, "plugins verify") for alias in [args.gateway, *args.runner]]
    failures: list[HostFailure] = []
    for alias, verb in checks:
        try:
            s.run(alias, f"{HOME}; {MEIVA} {verb}", effect=True)
        except FailedError as exc:
            failures.append(HostFailure(alias, verb, str(exc)))
    return failures


def _outcome(alias: str, failures: list[HostFailure], skipped: dict[str, str]) -> str:
    failed = [failure for failure in failures if failure.alias == alias]
    if failed:
        return "failed at " + "; ".join(f"{failure.step!r}: {failure.reason}" for failure in failed)
    if alias in skipped:
        return f"skipped: {skipped[alias]}"
    return "serving"


# meiva:note
# One line per listed host, whatever the others did: serving, the step it failed at, or why the
# window could not verify it at all.
def outcome(alias: str, failures: list[HostFailure], skipped: dict[str, str]) -> str:
    failed = [failure for failure in failures if failure.alias == alias]
    if failed:
        return "failed at " + "; ".join(f"{failure.step!r}: {failure.reason}" for failure in failed)
    if alias in skipped:
        return f"skipped: {skipped[alias]}"
    return "serving"


def verify(
    s: Runner,
    args: argparse.Namespace,
    *,
    hosts: list[str],
    started: set[str],
    program: str,
    gateway_stdin: str,
    clock: Any = time,
) -> tuple[list[HostFailure], dict[str, str], str]:
    # meiva:note
    # Roster, then one smoke per serving machine, then refresh and drift -- each host's answer
    # recorded on its own line. A host the window already knows failed to start is expected to
    # be offline, so one failure never costs the rest the whole roster timeout.
    failures: list[HostFailure] = []
    skipped: dict[str, str] = {}
    commit = ""
    if args.dry_run:
        s.run(args.gateway, f"{program} smoke MACHINE {args.smoke_timeout}  # each agent-runner")
        skipped.update(dict.fromkeys(hosts, "dry run"))
    else:
        try:
            names = machine_names(s, hosts)
            failed_names = frozenset(names[a] for a in hosts if a not in started)
            rows, commit = roster(
                s, args, names, program, gateway_stdin, clock=clock, expect_offline=failed_names
            )
        except FailedError as exc:
            failures += [HostFailure(alias, "roster", str(exc)) for alias in hosts]
        else:
            for name in [row["name"] for row in rows if row["serve_agent_runner"]]:
                owner = next(alias for alias, value in names.items() if value == name)
                if owner not in started:
                    skipped.setdefault(owner, "start failed")
                    continue
                smoke = f"{program} smoke {shlex.quote(name)} {args.smoke_timeout}"
                try:
                    s.run(args.gateway, smoke, stdin=gateway_stdin)
                except FailedError as exc:
                    failures.append(HostFailure(owner, "smoke", str(exc)))
    failures += refresh(s, args)
    failures += drift_checks(s, args)
    return failures, skipped, commit

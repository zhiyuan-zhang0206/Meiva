# meiva:note
# The down half of a window: refuse what cannot work, name what the stops would end, stop every
# runner and then the gateway, and switch every checkout to NEW. A failed stop brings back the
# hosts this run already stopped. Each question a step asks of a host is decided in `plan/`; this
# module reads the hosts, runs the commands and says the lines those decisions return.
# meiva:link cli/docs/fleet-update.meiva.okf.md

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

from .choices import forwarded_flags, message_flag, start_flags
from .fleet_gate import baselines, probe_helper, probe_host
from .oneshot import start_script
from .plan.gateway_commands import pre_verify_script
from .plan.helper_gate import rebuilding_hosts, refusals
from .plan.host_commands import (
    OP_EVIDENCE,
    fetch_command,
    stop_command,
    switch_command,
)
from .plan.host_probe import host_refusals
from .plan.journal import HostEvidence, parse_evidence, stop_completed
from .plan.old_target import (
    OldTarget,
    old_commit,
    python_change_reason,
    unfetched_old_reason,
)
from .plan.recovery import Recovery, recovery_action, recovery_report
from .plan.stop_wave import StopChoices, stop_steps
from .plan.stored_schedules import schedule_check
from .plan.wave_order import start_order
from .programs import (
    CHECK_ROWS_SCRIPT,
    VERIFY_SCRIPTS,
    announce_leases,
    program_text,
    run_silence,
)
from .session import FailedError, RefusedError, Session

_WATCHED = (
    "migrations/",
    "services/desktop/permissions_helper/helper",
    "uv.lock",
    "ui/web/package-lock.json",
    ".python-version",
)

_STOP_HINT = (
    "\nOn that host retry `meiva stop` with the same arguments this `down` gave it (the logged "
    "command above), confirm `run/lifecycle-op.json` records a completed stop (operation `stop`, "
    "complete, rc 0), then rerun `down`."
)


def _git(*args: str, repo: Path) -> subprocess.CompletedProcess[str]:
    command = ["git", "-C", str(repo), *args]
    return subprocess.run(command, capture_output=True, text=True, check=False)


def host_evidence(s: Session, alias: str) -> HostEvidence:
    return parse_evidence(s.run(alias, OP_EVIDENCE, effect=False))


# meiva:note
# OLD only feeds the `.python-version` refusal: it decides nothing about what stops or starts.
def _old_target(s: Session, args: argparse.Namespace, heads: dict[str, str], new: str) -> OldTarget:
    old = old_commit(heads, new)
    if old.reasons or old.resolved is None:
        return old
    if _git("cat-file", "-e", f"{old.resolved}^{{commit}}", repo=s.repo).returncode:
        return OldTarget((unfetched_old_reason(old.resolved, str(s.repo)),), None)
    stat = _git("diff", "--stat", old.resolved, new, "--", *_WATCHED, repo=s.repo).stdout.rstrip()
    s.say(stat or "no change under " + " ".join(_WATCHED))
    python = _git("diff", "--quiet", old.resolved, new, "--", ".python-version", repo=s.repo)
    allowed = args.python == "--allow-python-change"
    reasons = python_change_reason(changed=bool(python.returncode), allowed=allowed)
    return OldTarget(reasons, old.resolved)


# meiva:link cli/docs/fleet-update-window/fleet-update-window.meiva.okf.md
def _preflight(
    s: Session, args: argparse.Namespace, new: str
) -> tuple[list[str], frozenset[str], frozenset[str]]:
    hosts = start_order(args.gateway, tuple(args.runner))
    reasons: list[str] = []
    heads: dict[str, str] = {}
    serving: list[str] = []
    for alias in hosts:
        facts, was_serving = probe_host(s, alias)
        heads[alias] = facts.get("head", "")
        try:
            reasons += host_refusals(alias, facts)
        except ValueError as exc:
            raise RefusedError(f"invalid host probe: {exc}") from exc
        if was_serving:
            serving.append(alias)
    old = _old_target(s, args, heads, new)
    if reasons or old.reasons:
        return [*reasons, *old.reasons], frozenset(serving), frozenset()
    # meiva:note
    # The helper gate reads NEW, so every host must hold it first. This fetch is a
    # non-destructive effect the window deliberately accepts before the gate: the gate's own
    # question is about that commit, and the cheap refusals above have already passed.
    if args.dry_run:
        s.say("helper gate: NOT evaluated (dry run: the target commit is not fetched)")
        return [], frozenset(serving), frozenset()
    for alias in hosts:
        s.run(alias, fetch_command(new))
    probes = {alias: probe_helper(s, alias, new) for alias in hosts}
    rebuild = bool(getattr(args, "rebuild_extras", False))
    table = baselines(s.repo)
    try:
        helper_reasons = refusals(probes, table, rebuild=rebuild)
        rebuilding = rebuilding_hosts(probes, rebuild=rebuild)
    except ValueError as exc:
        raise RefusedError(f"invalid helper probe: {exc}") from exc
    return list(helper_reasons), frozenset(serving), rebuilding


# meiva:link cli/docs/fleet-update-window/fleet-update-window.meiva.okf.md
def _stored_schedules(s: Session, args: argparse.Namespace, new: str) -> None:
    out = s.run(
        args.gateway,
        pre_verify_script(
            program_text(VERIFY_SCRIPTS / "fleet_pre_verify.sh"),
            new=new,
            read_rows=program_text(VERIFY_SCRIPTS / "schedules_verify_read_rows.py"),
            check_rows=program_text(CHECK_ROWS_SCRIPT),
        ),
        effect=False,
    )
    check = schedule_check(out, allow_red=args.red == "--allow-red-schedules", new=new)
    for note in check.notes:
        s.say(note)
    if check.refusal is not None:
        raise RefusedError(check.refusal)


def _restart(s: Session, args: argparse.Namespace, alias: str) -> None:
    script = start_script(
        macos=s.os[alias] == "Darwin",
        alias=alias,
        stamp=s.stamp,
        timeout_s=args.start_timeout,
        flags=start_flags(args),
    )
    s.run(alias, script)


# meiva:note
# A failed stop must not leave the cluster half stopped. `plan.recovery` decides which hosts
# come back; the start is `meiva start` with the operator's explicit selection, never `--boot`:
# a boot attempt spends the boot budget, and on macOS the failing attempt that spends it exits 0
# (launchd has no attempt limit), so a failed recovery would read as success and never reach
# `needs manual:`.
# meiva:link cli/docs/fleet-update-window/failed-stop-recovery.meiva.okf.md
def _recover_after_failed_stop(
    s: Session,
    args: argparse.Namespace,
    serving: frozenset[str],
    stopped: tuple[str, ...],
    failed: str,
) -> None:
    recovered: list[str] = []
    down: list[str] = []
    for alias in start_order(args.gateway, tuple(args.runner)):
        if alias not in serving:
            continue
        try:
            evidence = host_evidence(s, alias)
        except FailedError as exc:
            down.append(alias)
            s.say(f"recovery: could not read {alias}'s evidence: {exc}")
            continue
        action = recovery_action(
            alias=alias,
            stopped=frozenset(stopped),
            failed=failed,
            evidence=evidence,
        )
        if action is Recovery.RUNNING:
            continue
        down.append(alias)
        if action is Recovery.LEFT_DOWN:
            continue
        try:
            _restart(s, args, alias)
            recovered.append(alias)
        except FailedError as exc:
            s.say(f"recovery: {exc}")
    for line in recovery_report(tuple(down), tuple(recovered)):
        s.say(line)


def _stop_wave(
    s: Session, args: argparse.Namespace, serving: frozenset[str], rebuilding: frozenset[str]
) -> None:
    choices = StopChoices(
        forwarded=(args.shell, args.browser, args.extras, *forwarded_flags(args, args.kept)),
        mode=args.mode,
        timeout=args.stop_timeout,
        infra=args.infra,
        message=message_flag(args),
    )
    stopped: list[str] = []
    current = args.gateway
    try:
        for step in stop_steps(
            gateway=args.gateway,
            runners=tuple(args.runner),
            rebuilding=rebuilding,
            choices=choices,
        ):
            current = step.alias
            if step.retires_extras:
                s.say(f"{step.alias}: retiring extras so its next start can rebuild the helper")
            s.run(step.alias, stop_command(step.flags))
            if not s.dry_run and not stop_completed(host_evidence(s, step.alias).journal):
                raise FailedError(
                    f"{step.alias}: stop exited 0 but its journal records no completed stop"
                )
            stopped.append(step.alias)
    except FailedError as exc:
        if not s.dry_run:
            _recover_after_failed_stop(s, args, serving, tuple(stopped), current)
        raise FailedError(f"{exc}{_STOP_HINT}") from exc


# meiva:link cli/docs/fleet-update.meiva.okf.md
def down(s: Session, args: argparse.Namespace) -> None:
    new = _git("rev-parse", "--verify", f"{args.new}^{{commit}}", repo=s.repo).stdout.strip()
    if not new:
        raise RefusedError(f"refused: {args.new} is not a commit in {s.repo}; git fetch origin")
    reasons, serving, rebuilding = _preflight(s, args, new)
    if reasons:
        raise RefusedError("refused: " + "; ".join(reasons))
    announce_leases(s, args.gateway, args.lease_confirm_seconds)
    _stored_schedules(s, args, new)
    silence_note = f" ({args.message_text})" if args.message_text else ""
    run_silence(
        s,
        args.gateway,
        "open",
        hours=args.silence_hours,
        comment=(
            f"fleet update to {new[:12]}: planned outage{silence_note}; `up` expires this silence"
        ),
    )
    _stop_wave(s, args, serving, rebuilding)
    for alias in start_order(args.gateway, tuple(args.runner)):
        s.run(alias, switch_command(new))
    next_step = "continuing with `up`" if args.half == "run" else "run `up` when ready"
    s.say(f"down complete: every host is stopped on {new[:12]}; {next_step}")

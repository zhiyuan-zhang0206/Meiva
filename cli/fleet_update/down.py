# meiva:note
# The down half of a window: refuse what cannot work, name what the stops would end, stop every
# runner and then the gateway, and switch every checkout to NEW. A failed stop brings back the
# hosts this run already stopped. Each question a step asks of a host is decided in `plan/`; this
# module reads the hosts, runs the commands and says the lines those decisions return.
# meiva:link cli/docs/fleet-update.meiva.okf.md

from __future__ import annotations

import argparse
from dataclasses import dataclass

from .choices import forwarded_flags, message_flag, start_flags
from .fleet_gate import baselines, git_in_repo, probe_helper, probe_host, resolve_target
from .oneshot import start_script
from .plan.gateway_commands import pre_verify_script
from .plan.helper_gate import rebuilding_hosts, refusals
from .plan.host_commands import (
    OP_EVIDENCE,
    fetch_command,
    stop_command,
    switch_command,
)
from .plan.host_probe import host_refusals, supports_stop_message
from .plan.journal import HostEvidence, parse_evidence, stop_completed
from .plan.old_target import (
    OldTarget,
    old_commit,
    python_change_reason,
    unfetched_old_reason,
)
from .plan.recovery import Recovery, recovery_action, recovery_report
from .plan.stop_wave import StopChoices, StopStep, stop_steps
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


def host_evidence(s: Session, alias: str) -> HostEvidence:
    return parse_evidence(s.run(alias, OP_EVIDENCE, effect=False))


# meiva:note
# OLD only feeds the `.python-version` refusal: it decides nothing about what stops or starts.
def _old_target(s: Session, args: argparse.Namespace, heads: dict[str, str], new: str) -> OldTarget:
    old = old_commit(heads, new)
    if old.reasons or old.resolved is None:
        return old
    if git_in_repo("cat-file", "-e", f"{old.resolved}^{{commit}}", repo=s.repo).returncode:
        return OldTarget((unfetched_old_reason(old.resolved, str(s.repo)),), None)
    stat = git_in_repo(
        "diff", "--stat", old.resolved, new, "--", *_WATCHED, repo=s.repo
    ).stdout.rstrip()
    s.say(stat or "no change under " + " ".join(_WATCHED))
    python = git_in_repo("diff", "--quiet", old.resolved, new, "--", ".python-version", repo=s.repo)
    allowed = args.python == "--allow-python-change"
    reasons = python_change_reason(changed=bool(python.returncode), allowed=allowed)
    return OldTarget(reasons, old.resolved)


@dataclass(frozen=True)
class _Preflight:
    reasons: tuple[str, ...]
    serving: frozenset[str]
    rebuilding: frozenset[str] = frozenset()
    legacy_stop: frozenset[str] = frozenset()


def _fetch_and_probe(s: Session, alias: str, new: str) -> dict[str, str]:
    s.run(alias, fetch_command(new))
    return probe_helper(s, alias, new)


# meiva:link cli/docs/fleet-update-window/fleet-update-window.meiva.okf.md
def _preflight(s: Session, args: argparse.Namespace, new: str) -> _Preflight:
    hosts = start_order(args.gateway, tuple(args.runner))
    probed = s.run_all(hosts, lambda alias: probe_host(s, alias))
    reasons: list[str] = []
    heads: dict[str, str] = {}
    serving: list[str] = []
    legacy_stop: list[str] = []
    for alias, (facts, was_serving) in zip(hosts, probed, strict=True):
        heads[alias] = facts.get("head", "")
        try:
            host_reasons = host_refusals(alias, facts)
            reasons += host_reasons
            if not host_reasons and not supports_stop_message(alias, facts):
                legacy_stop.append(alias)
        except ValueError as exc:
            raise RefusedError(f"invalid host probe: {exc}") from exc
        if was_serving:
            serving.append(alias)
    old = _old_target(s, args, heads, new)
    if reasons or old.reasons:
        return _Preflight((*reasons, *old.reasons), frozenset(serving))
    # meiva:note
    # The helper gate reads NEW, so every host must hold it first. This fetch is a
    # non-destructive effect the window deliberately accepts before the gate: the gate's own
    # question is about that commit, and the cheap refusals above have already passed.
    if args.dry_run:
        s.say("helper gate: NOT evaluated (dry run: the target commit is not fetched)")
        return _Preflight(
            (),
            frozenset(serving),
            legacy_stop=frozenset(legacy_stop),
        )
    helper_facts = s.run_all(hosts, lambda alias: _fetch_and_probe(s, alias, new))
    probes = dict(zip(hosts, helper_facts, strict=True))
    rebuild = bool(getattr(args, "rebuild_extras", False))
    table = baselines(s.repo)
    try:
        helper_reasons = refusals(probes, table, rebuild=rebuild)
        rebuilding = rebuilding_hosts(probes, rebuild=rebuild)
    except ValueError as exc:
        raise RefusedError(f"invalid helper probe: {exc}") from exc
    return _Preflight(
        tuple(helper_reasons),
        frozenset(serving),
        rebuilding=rebuilding,
        legacy_stop=frozenset(legacy_stop),
    )


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


def _restart_wave(
    s: Session, args: argparse.Namespace, to_restart: tuple[str, ...]
) -> frozenset[str]:
    recovered: set[str] = set()
    if args.gateway in to_restart:
        try:
            _restart(s, args, args.gateway)
            recovered.add(args.gateway)
        except FailedError as exc:
            s.say(f"recovery: {exc}")
    runners = [alias for alias in to_restart if alias != args.gateway]
    for alias, outcome in zip(
        runners, s.gather(runners, lambda alias: _restart(s, args, alias)), strict=True
    ):
        if isinstance(outcome, FailedError):
            s.say(f"recovery: {outcome}")
        else:
            recovered.add(alias)
    return frozenset(recovered)


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
    stopped: frozenset[str],
    failed: frozenset[str],
) -> None:
    serving_hosts = tuple(
        alias for alias in start_order(args.gateway, tuple(args.runner)) if alias in serving
    )
    evidences = s.gather(serving_hosts, lambda alias: host_evidence(s, alias))
    down: list[str] = []
    to_restart: list[str] = []
    for alias, evidence in zip(serving_hosts, evidences, strict=True):
        if isinstance(evidence, FailedError):
            down.append(alias)
            s.say(f"recovery: could not read {alias}'s evidence: {evidence}")
            continue
        action = recovery_action(
            alias=alias,
            stopped=stopped,
            failed=failed,
            evidence=evidence,
        )
        if action is Recovery.RUNNING:
            continue
        down.append(alias)
        if action is Recovery.RESTART:
            to_restart.append(alias)
    recovered_set = _restart_wave(s, args, tuple(to_restart))
    recovered = tuple(alias for alias in down if alias in recovered_set)
    for line in recovery_report(tuple(down), recovered):
        s.say(line)


def _stop_one(s: Session, step: StopStep) -> None:
    if step.retires_extras:
        s.say(f"{step.alias}: retiring extras so its next start can rebuild the helper")
    s.run(step.alias, stop_command(step.flags))
    if not s.dry_run and not stop_completed(host_evidence(s, step.alias).journal):
        raise FailedError(f"{step.alias}: stop exited 0 but its journal records no completed stop")


def _stop_wave(s: Session, args: argparse.Namespace, pre: _Preflight) -> None:
    choices = StopChoices(
        forwarded=(args.shell, args.browser, args.extras, *forwarded_flags(args, args.kept)),
        mode=args.mode,
        timeout=args.stop_timeout,
        infra=args.infra,
        message=message_flag(args),
        legacy_message_hosts=pre.legacy_stop,
    )
    steps = stop_steps(
        gateway=args.gateway,
        runners=tuple(args.runner),
        rebuilding=pre.rebuilding,
        choices=choices,
    )
    runner_steps, gateway_step = steps[:-1], steps[-1]
    stopped: list[str] = []
    failed: list[str] = []
    errors: list[FailedError] = []
    for step, outcome in zip(
        runner_steps, s.gather(runner_steps, lambda step: _stop_one(s, step)), strict=True
    ):
        if isinstance(outcome, FailedError):
            failed.append(step.alias)
            errors.append(outcome)
        else:
            stopped.append(step.alias)
    if errors:
        if not s.dry_run:
            _recover_after_failed_stop(s, args, pre.serving, frozenset(stopped), frozenset(failed))
        summary = "; ".join(str(err) for err in errors)
        raise FailedError(f"{summary}{_STOP_HINT}") from errors[0]
    try:
        _stop_one(s, gateway_step)
    except FailedError as exc:
        if not s.dry_run:
            _recover_after_failed_stop(
                s, args, pre.serving, frozenset(stopped), frozenset({gateway_step.alias})
            )
        raise FailedError(f"{exc}{_STOP_HINT}") from exc


# meiva:link cli/docs/fleet-update.meiva.okf.md
def down(s: Session, args: argparse.Namespace) -> None:
    pre_resolved: str | None = getattr(args, "resolved_new", None)
    new = (
        pre_resolved
        if pre_resolved is not None
        else resolve_target(s.repo, args.target_request).sha
    )
    pre = _preflight(s, args, new)
    if pre.reasons:
        raise RefusedError("refused: " + "; ".join(pre.reasons))
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
    _stop_wave(s, args, pre)
    s.run_all(
        start_order(args.gateway, tuple(args.runner)),
        lambda alias: s.run(alias, switch_command(new)),
    )
    next_step = "continuing with `up`" if args.half == "run" else "run `up` when ready"
    s.say(f"down complete: every host is stopped on {new[:12]}; {next_step}")

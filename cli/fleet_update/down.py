# meiva:note
# The down half of a window: refuse what cannot work, name what the stops would end, stop every
# runner and then the gateway, and switch every checkout to NEW. Every selected host must pass
# preparation. Once the stop wave starts, a runner with a command failure can be deferred.
# A failed gateway stop or switch ends the window and
# brings every host this run stopped back on the commit it ran before. Each question a step asks
# of a host is decided in `plan/`; this module reads the hosts, runs the commands and says the
# lines those decisions return.
# meiva:link cli/docs/fleet-update.md

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace

from .choices import DownChoices, FleetArgs, forwarded_flags, message_flag, start_flags
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
from .plan.host_failures import NOT_ATTEMPTED_WITHOUT_GATEWAY, HostFailure
from .plan.host_probe import host_refusals, supports_stop_message
from .plan.journal import HostEvidence, parse_evidence, stop_completed
from .plan.old_target import (
    OldTarget,
    old_commit,
    python_change_reason,
    unfetched_old_reason,
)
from .plan.recovery import Recovery, recovery_action, recovery_report
from .plan.root_generation import root_check_script, root_is_kept, root_refusal
from .plan.stop_wave import StopChoices, StopStep, stop_steps
from .plan.stored_schedules import schedule_check
from .plan.wave_order import start_order
from .prestage import prestage_command
from .probes.collect import prepare_receipts
from .programs import (
    CHECK_ROWS_SCRIPT,
    VERIFY_SCRIPTS,
    program_text,
    run_silence,
)
from .session import FailedError, RefusedError, Session

_WATCHED = (
    "migrations/",
    "services/desktop/permissions_helper/helper",
    "uv.lock",
    "ui/web/pnpm-lock.yaml",
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
# The choices of the `down` half; only the `run` and `down` halves carry them.
def _chosen(args: FleetArgs) -> DownChoices:
    if args.down is None:
        raise RefusedError("refused: this half carries no stop choices")
    return args.down


# meiva:note
# OLD only feeds the `.python-version` refusal: it decides nothing about what stops or starts.
def _old_target(s: Session, args: FleetArgs, heads: dict[str, str], new: str) -> OldTarget:
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
    allowed = _chosen(args).python == "--allow-python-change"
    reasons = python_change_reason(changed=bool(python.returncode), allowed=allowed)
    return OldTarget(reasons, old.resolved)


@dataclass(frozen=True)
class _Preflight:
    reasons: tuple[str, ...]
    serving: frozenset[str]
    rebuilding: frozenset[str] = frozenset()
    legacy_stop: frozenset[str] = frozenset()
    heads: Mapping[str, str] = field(default_factory=dict[str, str])
    running_roots: frozenset[str] = frozenset()
    schema_changes: bool = False


def _fetch_and_probe(s: Session, alias: str, new: str) -> dict[str, str]:
    s.run(alias, fetch_command(new))
    return probe_helper(s, alias, new)


def _selected_hosts(args: FleetArgs) -> tuple[str, ...]:
    return start_order(args.gateway, args.runner)


def _hosts(s: Session, args: FleetArgs) -> tuple[str, ...]:
    return start_order(args.gateway, s.live(args.runner))


@dataclass(frozen=True)
class _Verdict:
    reasons: tuple[str, ...]
    legacy_stop: bool


def _probe_verdict(alias: str, facts: Mapping[str, str]) -> _Verdict:
    try:
        reasons = host_refusals(alias, facts)
        legacy = not reasons and not supports_stop_message(alias, facts)
    except ValueError as exc:
        raise RefusedError(f"invalid host probe: {exc}") from exc
    return _Verdict(reasons, legacy)


# meiva:link cli/docs/fleet-update-window/fleet-update-window.md
def _preflight(s: Session, args: FleetArgs, new: str) -> _Preflight:
    probed = s.gather_hosts(
        _selected_hosts(args),
        gateway=args.gateway,
        step="preflight",
        work=lambda alias: probe_host(s, alias),
    )
    reasons: list[str] = []
    heads: dict[str, str] = {}
    serving: list[str] = []
    legacy_stop: list[str] = []
    running_roots: set[str] = set()
    for alias, (facts, was_serving) in probed.items():
        verdict = _probe_verdict(alias, facts)
        reasons += verdict.reasons
        heads[alias] = facts.get("head", "")
        if verdict.legacy_stop:
            legacy_stop.append(alias)
        if facts.get("root") == "up":
            running_roots.add(alias)
        if was_serving:
            serving.append(alias)
    old = _old_target(s, args, heads, new)
    if reasons or old.reasons:
        return _Preflight((*reasons, *old.reasons), frozenset(serving), heads=heads)
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
            heads=heads,
            running_roots=frozenset(running_roots),
        )
    probes = _fetch_gateway_first(s, args, new)
    rebuild = args.rebuild_extras
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
        heads=heads,
        running_roots=frozenset(running_roots),
    )


def _fetch_gateway_first(s: Session, args: FleetArgs, new: str) -> dict[str, dict[str, str]]:
    probes = {args.gateway: _fetch_and_probe(s, args.gateway, new)}
    probes.update(
        s.gather_hosts(
            args.runner,
            gateway=args.gateway,
            step="fetch",
            work=lambda alias: _fetch_and_probe(s, alias, new),
        )
    )
    return probes


# meiva:link cli/docs/fleet-update-window/fleet-update-window.md
def _stored_schedules(s: Session, args: FleetArgs, new: str) -> str:
    if args.dry_run:
        s.say("stored-schedule verify: skipped (dry run does not stage a target worktree)")
        return "skipped"
    out = s.run(
        args.gateway,
        pre_verify_script(
            program_text(VERIFY_SCRIPTS / "fleet_pre_verify.sh"),
            new=new,
            read_rows=program_text(VERIFY_SCRIPTS / "schedules_verify_read_rows.py"),
            check_rows=program_text(CHECK_ROWS_SCRIPT),
            max_age_min=int(_chosen(args).silence_hours * 60),
        ),
    )
    check = schedule_check(out, allow_red=_chosen(args).red == "--allow-red-schedules", new=new)
    for note in check.notes:
        s.say(note)
    if check.refusal is not None:
        raise RefusedError(check.refusal)
    return check.status


# meiva:note
# Before anything stops: could the root each host keeps serve NEW? A start over a live root of
# another generation is refused, and `up` only meets that refusal after every service stopped.
# meiva:link cli/docs/fleet-update-window/fleet-update-window.md
def _kept_roots(s: Session, args: FleetArgs, new: str, pre: _Preflight) -> None:
    chosen = _chosen(args)
    if not root_is_kept(shell=chosen.shell, kept=chosen.kept):
        return
    if args.dry_run:
        s.say("kept-root check: skipped (dry run does not stage a target worktree)")
        return
    script = root_check_script(
        program_text(VERIFY_SCRIPTS / "fleet_pre_root_check.sh"),
        new=new,
        selection=args.selection,
        names=args.named.get(args.selection, ()),
    )
    outputs = s.gather_hosts(
        _selected_hosts(args),
        gateway=args.gateway,
        step="root check",
        work=lambda alias: s.run(alias, script),
    )
    refusals: list[str] = []
    for alias, output in outputs.items():
        refusal = root_refusal(
            alias,
            output,
            new=new,
            was_running=alias in pre.running_roots,
        )
        if refusal is not None:
            refusals.append(refusal)
    if refusals:
        raise RefusedError("refused: " + "; ".join(refusals))


def _restart(s: Session, args: FleetArgs, alias: str) -> None:
    script = start_script(
        macos=s.os[alias] == "Darwin",
        alias=alias,
        stamp=s.stamp,
        timeout_s=args.start_timeout,
        flags=start_flags(args),
    )
    s.run(alias, script)


def _restart_wave(s: Session, args: FleetArgs, to_restart: tuple[str, ...]) -> frozenset[str]:
    recovered: set[str] = set()
    if args.gateway in to_restart:
        try:
            _restart(s, args, args.gateway)
            recovered.add(args.gateway)
        except FailedError as exc:
            s.say(f"recovery: {exc}")
            s.say(f"recovery: runners not started: {NOT_ATTEMPTED_WITHOUT_GATEWAY}")
            return frozenset(recovered)
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
# A failed gateway stop must not leave the cluster half stopped. `plan.recovery` decides which
# hosts come back (a deferred runner is outside the window and is never touched); the start is `meiva start` with the operator's explicit selection, never `--boot`:
# a boot attempt spends the boot budget, and on macOS the failing attempt that spends it exits 0
# (launchd has no attempt limit), so a failed recovery would read as success and never reach
# `needs manual:`.
# meiva:link cli/docs/fleet-update-window/failed-stop-recovery.md
def _recover_after_failed_stop(
    s: Session,
    args: FleetArgs,
    serving: frozenset[str],
    stopped: frozenset[str],
    failed: frozenset[str],
) -> None:
    serving_hosts = tuple(alias for alias in _hosts(s, args) if alias in serving)
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


def _stop_wave(s: Session, args: FleetArgs, pre: _Preflight) -> None:
    chosen = _chosen(args)
    choices = StopChoices(
        forwarded=(
            chosen.shell,
            chosen.browser,
            chosen.extras,
            *forwarded_flags(args, chosen.kept),
        ),
        mode=chosen.mode,
        timeout=chosen.stop_timeout,
        infra=chosen.infra,
        message=message_flag(args),
        legacy_message_hosts=pre.legacy_stop,
    )
    steps = stop_steps(
        gateway=args.gateway,
        runners=s.live(args.runner),
        rebuilding=pre.rebuilding,
        choices=choices,
    )
    runner_steps, gateway_step = steps[:-1], steps[-1]
    stopped: list[str] = []
    failed: list[HostFailure] = []
    for step, outcome in zip(
        runner_steps, s.gather(runner_steps, lambda step: _stop_one(s, step)), strict=True
    ):
        if isinstance(outcome, FailedError):
            failed.append(HostFailure(step.alias, "stop", str(outcome)))
        else:
            stopped.append(step.alias)
    if failed and pre.schema_changes:
        _recover_after_failed_stop(
            s, args, pre.serving, frozenset(stopped), frozenset(item.alias for item in failed)
        )
        raise FailedError(
            "schema-changing rollout stopped before gateway: "
            + "; ".join(item.line() for item in failed)
        )
    for failure in failed:
        s.defer(failure)
    try:
        _stop_one(s, gateway_step)
    except FailedError as exc:
        if not s.dry_run:
            _recover_after_failed_stop(
                s, args, pre.serving, frozenset(stopped), frozenset({gateway_step.alias})
            )
        raise FailedError(f"{exc}{_STOP_HINT}") from exc


def _abort_after_failed_switch(
    s: Session, args: FleetArgs, pre: _Preflight, error: FailedError
) -> FailedError:
    hosts = _hosts(s, args)
    s.say("recovery: the gateway could not switch; returning every host to the commit it ran")
    outcomes = s.gather(hosts, lambda alias: s.run(alias, switch_command(pre.heads[alias])))
    restored: list[str] = []
    for alias, outcome in zip(hosts, outcomes, strict=True):
        if isinstance(outcome, FailedError):
            s.say(f"recovery: {outcome}")
        elif alias in pre.serving:
            restored.append(alias)
    if restored and args.gateway not in restored:
        s.say(f"recovery: runners not started: {NOT_ATTEMPTED_WITHOUT_GATEWAY}")
        restored = []
    recovered = _restart_wave(s, args, tuple(restored))
    down = tuple(alias for alias in hosts if alias in pre.serving)
    for line in recovery_report(down, tuple(alias for alias in down if alias in recovered)):
        s.say(line)
    return FailedError(f"{error}; the update is aborted and the old version restored where listed")


def _switch_wave(s: Session, args: FleetArgs, pre: _Preflight, new: str) -> None:
    hosts = _hosts(s, args)
    outcomes = s.gather(hosts, lambda alias: s.run(alias, switch_command(new)))
    gateway_error: FailedError | None = None
    for alias, outcome in zip(hosts, outcomes, strict=True):
        if not isinstance(outcome, FailedError):
            continue
        if alias == args.gateway:
            gateway_error = outcome
        else:
            s.defer(HostFailure(alias, "switch", str(outcome)))
    if gateway_error is not None:
        if s.dry_run:
            raise gateway_error
        raise _abort_after_failed_switch(s, args, pre, gateway_error) from gateway_error


# meiva:link cli/docs/fleet-update.md
def prepare(s: Session, args: FleetArgs, resolved_new: str | None) -> _Preflight:
    if resolved_new is not None:
        new = resolved_new
    elif args.target_request is not None:
        new = resolve_target(s.repo, args.target_request).sha
    else:
        raise RefusedError("refused: no target to resolve")
    with s.timed("preflight"):
        pre = _preflight(s, args, new)
    if pre.reasons:
        raise RefusedError("refused: " + "; ".join(pre.reasons))
    with s.timed("kept-root check"):
        _kept_roots(s, args, new, pre)
    with s.timed("stored-schedule verify"):
        schedules = _stored_schedules(s, args, new)
    hosts = _selected_hosts(args)
    with s.timed("prestage"):
        _ = s.gather_hosts(
            hosts,
            gateway=args.gateway,
            step="prestage",
            work=lambda alias: s.run(alias, prestage_command(new)),
        )
    with s.timed("domain compatibility"):
        schema_changes = prepare_receipts(s, args, hosts, new, schedules=schedules)
    return replace(pre, schema_changes=schema_changes)


def down(
    s: Session,
    args: FleetArgs,
    resolved_new: str | None,
    *,
    before_stop: Callable[[], None] | None = None,
) -> None:
    pre = prepare(s, args, resolved_new)
    if resolved_new is None:
        raise RefusedError("no target to resolve")
    new = resolved_new
    if before_stop is not None:
        before_stop()
    silence_note = f" ({args.message_text})" if args.message_text else ""
    with s.timed("silence open"):
        run_silence(
            s,
            args.gateway,
            "open",
            hours=_chosen(args).silence_hours,
            comment=(
                f"fleet update to {new[:12]}: planned outage{silence_note}; "
                "`up` expires this silence"
            ),
        )
    with s.timed("stop wave"):
        _stop_wave(s, args, pre)
    with s.timed("switch wave"):
        _switch_wave(s, args, pre, new)
    next_step = "continuing with `up`" if args.half == "run" else "run `up` when ready"
    left_out = f" except {', '.join(s.deferred)}" if s.deferred else ""
    s.say(f"down complete: every host{left_out} is stopped on {new[:12]}; {next_step}")

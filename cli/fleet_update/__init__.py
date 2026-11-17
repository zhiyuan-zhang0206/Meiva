# meiva:note
# `meiva update {run,down,up}` — cluster-wide source-mode rollout over SSH.
# meiva:link cli/docs/fleet-update.md

from __future__ import annotations

import argparse
import sys
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path

from base.paths.scratch import ScratchSpace

from . import choices
from .down import down
from .fleet_gate import resolve_target
from .inputs import UpdateWindowInputs
from .plan.host_failures import PARTIAL_FAILURE_EXIT, incomplete_summary
from .plan.operation import OperationIdentity, gate_for
from .session import FailedError, Pacing, RefusedError, Session, Timers
from .up import up


def _halves(half: str) -> tuple[str, ...]:
    return ("down", "up") if half == "run" else (half,)


def _identity(args: choices.FleetArgs, operation_id: str, initiator: str) -> OperationIdentity:
    request = args.target_request
    if request is None:
        return OperationIdentity(
            operation_id, initiator, "overridden", "up-only:no target decision in this half"
        )
    gate, reason = gate_for(request.target_mode, branch=request.branch)
    return OperationIdentity(operation_id, initiator, gate, reason)


def _new_target(s: Session, args: choices.FleetArgs, pinned: str | None) -> str | None:
    if args.half == "up":
        return None
    if args.target_request is None:
        raise RefusedError("refused: no target to resolve")
    if pinned is not None:
        return pinned
    resolved = resolve_target(s.repo, args.target_request)
    s.say(resolved.notice)
    return resolved.sha


# meiva:note
# One window, from validated `FleetArgs` to exit code. Every ambient fact arrives as a value, read
# once by the entry point: the repository the NEW commit is resolved in, the environment (the PATH
# that finds `ssh`, and the agent marker that refuses a run from inside an agent), the time the log
# and the one-shot job names are stamped with, how often to look again while waiting, and the clock
# those waits are spent on.
# Exit codes: 0 done, 1 failed after the first effect, 2 refused before any, 3 a partial `up`.
# meiva:link cli/docs/fleet-update.md
def run_window(
    args: choices.FleetArgs,
    *,
    repo: Path,
    env: Mapping[str, str],
    now: datetime,
    pacing: Pacing,
    timers: Timers,
    operation_id: str,
    initiator: str,
    schedule_inputs: UpdateWindowInputs | None = None,
    pinned_new: str | None = None,
    strict: bool = False,
    delivered_to: set[int] | None = None,
) -> int:
    # meiva:note
    # The agent-shell marker is spelled out here; its owner is
    # `base.host.env.agent_env.AGENT_ID_ENV` (a test pins the two together).
    if "MEIVA_AGENT_ID" in env and args.at is None:
        print(
            "refused: run from a plain login shell or an OS-owned job, not an Meiva "
            "agent's shell (a stop closes it); submit the job and return so the agent can drain"
        )
        return 2
    # meiva:note
    # The window mints one identity and stamps it on every host command (task #6024). The gate
    # verdict comes from the target mode the operator asked for, not from what resolution later
    # finds: `track-branch` ships an unreleased branch and is the one mode that records
    # `overridden`, which is the fact a reader needs when asking why a release gate did not
    # stand in the way. An `up`-only half makes no target decision — its gate was decided by the
    # `down` half that switched the checkouts — so it says so instead of claiming a verdict.
    if args.at is not None:
        from .scheduling import validate

        problem = validate(args, now)
        if problem is not None:
            print(f"refused: {problem}")
            return 2
        initiator = str(args.source)
    operation = _identity(args, operation_id, initiator)
    s = Session(
        log_dir=args.log_dir,
        half=args.half,
        dry_run=args.dry_run,
        repo=repo,
        env=env,
        now=now,
        pacing=pacing,
        timers=timers,
        operation=operation,
    )
    phase = "down" if args.half == "run" else args.half
    resolved_new: str | None = None
    try:
        resolved_new = _new_target(s, args, pinned_new)
        hosts = [args.gateway, *args.runner]
        unames = s.run_all(hosts, lambda host: s.run(host, "uname -s", effect=False).split()[-1])
        s.os = dict(zip(hosts, unames, strict=True))
        if args.at is not None:
            from .scheduling import prepare_submit

            prepare_submit(s, args, schedule_inputs, resolved_new)
            return 0
        for phase in _halves(args.half):
            s.say(f"phase: {phase}")
            if phase == "down":
                from .scheduling.agents import require_quiescent

                down(
                    s,
                    args,
                    resolved_new,
                    before_stop=(
                        lambda: require_quiescent(
                            s, args.gateway, initiator, operation_id, delivered_to=delivered_to
                        )
                    )
                    if strict
                    else None,
                )
                continue
            failures = up(s, args)
            if failures:
                for line in incomplete_summary(tuple(failures)):
                    s.say(line)
                return PARTIAL_FAILURE_EXIT
    except FailedError as exc:
        s.say(
            f"FAILED: {exc}\nFailed phase: {phase}. Nothing rolled back. "
            f"Fix it, then rerun `{phase}` (idempotent)."
        )
        return 2 if isinstance(exc, RefusedError) else 1
    finally:
        s.log.close()
    return 0


def run_parsed(args: argparse.Namespace, inputs: UpdateWindowInputs) -> int:
    if vars(args).get("half") in ("status", "cancel"):
        from .scheduling import manage

        return manage(args, inputs)
    outcome = choices.validate_parsed(
        args, inputs.env, python=inputs.python, scratch=inputs.scratch
    )
    if isinstance(outcome, str):
        sys.stderr.write(outcome)
        return 2
    return run_window(
        outcome,
        repo=inputs.repo,
        env=inputs.env,
        now=inputs.now,
        pacing=inputs.pacing,
        timers=inputs.timers,
        operation_id=inputs.operation_id,
        initiator=inputs.initiator,
        schedule_inputs=inputs,
    )


def main(
    argv: list[str],
    *,
    repo: Path,
    env: Mapping[str, str],
    now: datetime,
    pacing: Pacing,
    timers: Timers,
    operation_id: str,
    initiator: str,
    python: str,
    scratch: ScratchSpace,
) -> int:
    args = choices.parse(argv, env, python=python, scratch=scratch)
    return run_window(
        args,
        repo=repo,
        env=env,
        now=now,
        pacing=pacing,
        timers=timers,
        operation_id=operation_id,
        initiator=initiator,
        schedule_inputs=UpdateWindowInputs(
            repo, env, now, pacing, timers, operation_id, initiator, python, scratch
        ),
    )

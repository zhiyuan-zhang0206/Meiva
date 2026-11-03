# meiva:note
# cli.fleet_update imports no application code — no base, cli, gateway, services, agent or meiva modules — so the halves run from a bare operator checkout.
# meiva:link cli/docs/fleet-update.meiva.okf.md

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from pathlib import Path

from . import choices
from .down import down
from .fleet_gate import resolve_target
from .plan.host_failures import PARTIAL_FAILURE_EXIT, incomplete_summary
from .plan.operation import OperationIdentity, gate_for
from .session import FailedError, Pacing, RefusedError, Session, Timers
from .up import up


# meiva:note
# One window, from argv to exit code. Every ambient fact arrives as a value, read once by the
# entry point: the repository the NEW commit is resolved in, the environment (the PATH that finds
# `ssh`, and the agent marker that refuses a run from inside an agent), the time the log and the
# one-shot job names are stamped with, how often to look again while waiting, the clock those
# waits are spent on, and the interpreter this window runs on -- the one the service-roster
# check, which decides whether a named service exists, starts as a child.
# Exit codes: 0 done, 1 failed after the first effect, 2 refused before any, 3 a partial `up`.
# meiva:link cli/docs/fleet-update.meiva.okf.md
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
) -> int:
    args = choices.parse(argv, env, python=python)
    # meiva:note
    # The updater imports no application module, so the agent-shell marker is spelled out here;
    # its owner is `base.host.env.agent_env.AGENT_ID_ENV` (a test pins the two together).
    if "MEIVA_AGENT_ID" in env:
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
    target_request = args.target_request
    if target_request is None:
        operation = OperationIdentity(
            operation_id=operation_id,
            initiator=initiator,
            gate="overridden",
            gate_reason="up-only:no target decision in this half",
        )
    else:
        gate, gate_reason = gate_for(target_request.target_mode, branch=target_request.branch)
        operation = OperationIdentity(
            operation_id=operation_id,
            initiator=initiator,
            gate=gate,
            gate_reason=gate_reason,
        )
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
        if args.half != "up":
            if target_request is None:
                # meiva:note
                # `choices.parse` refuses a half that needs a target and was given none, so this
                # arm is unreachable in a real window; it refuses rather than letting a `None`
                # reach the resolver, where the failure would name a type instead of the choice.
                raise FailedError("refused: no target to resolve")
            resolved = resolve_target(repo, target_request)
            resolved_new = resolved.sha
            s.say(resolved.notice)
        hosts = [args.gateway, *args.runner]
        unames = s.run_all(hosts, lambda host: s.run(host, "uname -s", effect=False).split()[-1])
        s.os = dict(zip(hosts, unames, strict=True))
        for phase in ("down", "up") if args.half == "run" else (args.half,):
            s.say(f"phase: {phase}")
            if phase == "down":
                down(s, args, resolved_new)
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

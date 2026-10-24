# meiva:note
# cli.fleet_update imports no application code — no base, cli, gateway, services, agent or meiva modules — so the halves run from a bare operator checkout.
# meiva:link cli/docs/fleet-update.meiva.okf.md

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from pathlib import Path

from . import choices
from .down import down
from .plan.host_failures import PARTIAL_FAILURE_EXIT, incomplete_summary
from .session import FailedError, Pacing, RefusedError, Session
from .up import up


# meiva:note
# One window, from argv to exit code. Every ambient fact arrives as a value, read once by the
# entry point: the repository the NEW commit is resolved in, the environment (the PATH that finds
# `ssh`, and the agent marker that refuses a run from inside an agent), the time the log and the
# one-shot job names are stamped with, and how often to look again while waiting.
# Exit codes: 0 done, 1 failed after the first effect, 2 refused before any, 3 a partial `up`.
# meiva:link cli/docs/fleet-update.meiva.okf.md
def main(
    argv: list[str], *, repo: Path, env: Mapping[str, str], now: datetime, pacing: Pacing
) -> int:
    args = choices.parse(argv, env)
    if "MEIVA_AGENT_ID" in env:
        print(
            "refused: run from a plain login shell or an OS-owned job, not an Meiva "
            "agent's shell (a stop closes it); submit the job and return so the agent can drain"
        )
        return 2
    s = Session(
        log_dir=args.log_dir,
        half=args.half,
        dry_run=args.dry_run,
        repo=repo,
        env=env,
        now=now,
        pacing=pacing,
    )
    phase = "down" if args.half == "run" else args.half
    try:
        hosts = [args.gateway, *args.runner]
        s.os = {host: s.run(host, "uname -s", effect=False).split()[-1] for host in hosts}
        for phase in ("down", "up") if args.half == "run" else (args.half,):
            s.say(f"phase: {phase}")
            if phase == "down":
                down(s, args)
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

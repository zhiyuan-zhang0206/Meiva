# meiva:note
# Starting one watcher: the generated files, the session, the launch line. The namespace module
# `meiva/watcher/__init__.py` builds a `SpawnRequest` (see `plans.py`) and a `SpawnDeps` (see
# `wiring.py`) and calls `run`. Everything `run` touches that is not its request arrives in the
# deps as values or as the agent's real shell sessions: the agent id, the directory for generated
# files, the machine facts a session starts from, the CLI that delivers the completion notice,
# the live session ids, the runner env and the interpreter. What the files and the line
# contain is decided in `launch_plan.py`.
# meiva:link meiva/watcher/docs/watcher.meiva.okf.md

from __future__ import annotations

import contextlib
import logging
from dataclasses import dataclass
from pathlib import Path

from meiva.shell import SessionHandle, background_plan, log_ring
from meiva.shell.background import allocate_output_path, write_launch_file
from meiva.shell.session_env import SessionHost
from meiva.shell.sessions import ShellSessions
from meiva.watcher import files, launch_plan
from meiva.watcher.launch_plan import SpawnRequest

__all__ = ["SpawnDeps", "run"]

logger = logging.getLogger(__name__)


# meiva:note
# The dependencies of `run`, nothing else. `live_session_ids` is read before the session is
# created; `workspace` is the agent's workspace, the session's starting directory and the parent
# of the output logs.
@dataclass(frozen=True)
class SpawnDeps:
    agent_id: int
    watchers_dir: Path
    shell: ShellSessions
    host: SessionHost
    workspace: Path
    cli: Path
    live_session_ids: frozenset[int]
    runner_env: dict[str, str]
    python: str


# meiva:note
# Start a watcher child running ``request.code``; return its handle.
#
# A watcher is nothing more than a shell session running a generated script - there is no
# separate registry or desired-state record
# (docs/decisions/runtime/updates/recovery/2026-09-27-watchers-are-never-restarted.md): list it,
# capture its output, renew its deadline, or kill it exactly like any other session via
# ``meiva.shell.sessions``. Re-registering the same schedule (`cron()` with the same
# expression/timezone, say) does not replace anything - it simply starts another independent
# session; nothing dedupes.
#
# The session sources a generated shell launcher, which runs the agent's script (written
# verbatim) and a generated bootstrap that inlines the agent identity, arms the optional watchdog
# and runs the script via runpy - so the command line typed into the session stays short and
# readable. Output is teed to a per-agent log file and session capture, and a completion notice
# (log path + output tail) is delivered from the shell level when the child exits, on every exit
# path - a crashed or hard-killed child cannot skip it. The session closes itself after the
# notice is delivered (the log file preserves the output); a notice that fails to send leaves the
# session open as the post-mortem site.
#
# The session's shell TTL IS this watcher's target deadline (user ruling 2026-09-14, task #3411),
# derived once, in `plans.py`, and carried by the request. Written as the system-side TRUE value:
# a 7-day standing cron is a normal watcher, exempt from the 24h user-session cap.
# `meiva.shell.sessions.renew` can move this same deadline later, exactly like any other
# session's TTL - but only the session's reclamation, never the generated script's own end, and a
# renewal call is itself capped at 24h.
#
# Stale files of earlier watchers are pruned before the session is created: a watcher reads its
# script + bootstrap exactly once at launch, so only a pair whose session id is not live is dead
# weight (see files.prune_stale). Everything after the session exists is undone if it fails: the
# generated files are removed and the session is killed, so a session whose launch never sent is
# not left as an idle login shell under the watcher's name until its TTL.
def run(request: SpawnRequest, deps: SpawnDeps) -> SessionHandle:
    notify = background_plan.validate_notify(request.notify)
    files.prune_stale(deps.watchers_dir, deps.live_session_ids)
    session_id, _session_name = deps.shell.create(
        request.name,
        cwd=deps.workspace,
        ttl=request.ttl_seconds,
        system=True,
        env_overrides=deps.runner_env,
        host=deps.host,
    )
    plan = launch_plan.plan_files(
        deps.watchers_dir, session_id, request, deps.agent_id, deps.python
    )
    try:
        plan.script_path.write_text(plan.script_text)
        plan.boot_path.write_text(plan.boot_text)
        output_path = allocate_output_path(
            session_id,
            request.name,
            log_dir=log_ring.output_dir(deps.workspace),
            live=deps.live_session_ids | {session_id},
        )
        line = launch_plan.launch_line(
            plan,
            agent_id=deps.agent_id,
            name=request.name,
            output_path=output_path,
            cli=deps.cli,
            notify=notify,
        )
        deps.shell.send(session_id, write_launch_file(line, plan.launch_path))
    except Exception:
        for path in (plan.launch_path, plan.boot_path, plan.script_path):
            with contextlib.suppress(OSError):
                path.unlink(missing_ok=True)
        logger.error("[watcher] failed to start session %s - killing it", session_id, exc_info=True)
        try:
            deps.shell.kill(session_id)
        except Exception:
            logger.warning(
                "[watcher] killing session %s after the failed start also failed; "
                "it stays until its TTL expires",
                session_id,
                exc_info=True,
            )
        raise
    return SessionHandle(session_id=session_id, output_path=str(output_path))

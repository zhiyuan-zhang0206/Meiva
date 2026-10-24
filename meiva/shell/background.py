# meiva:link meiva/shell/docs/shell.meiva.okf.md
from __future__ import annotations

import contextlib
import sys
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path

from base.log import logger
from meiva.shell import background_plan, log_ring
from meiva.shell.handle import SessionHandle
from meiva.shell.session_env import SessionHost
from meiva.shell.sessions import ShellSessions


# meiva:note
# Path of the `meiva` CLI next to the running interpreter (installed into the venv's bin by `uv sync`). The session's own PATH is not trusted, since a login shell re-initializes it, so the generated line must carry an absolute path. The SDK entry reads it once and passes it down.
def cli_path() -> Path:
    p = Path(sys.executable).with_name("meiva")
    if not p.exists():
        raise RuntimeError(
            f"meiva CLI not found at {p} — background completion notices need the "
            "CLI installed alongside the interpreter (uv sync installs it)"
        )
    return p


# meiva:note
# Create (touch) and return the log file for one background run, pruning the directory's oldest logs beyond the ring size. Touching the file up front makes the path immediately readable, so progress can be tailed mid-run. `live` is the ids of the agent's live sessions, whose logs survive the ring cap.
def allocate_output_path(
    session_id: int, name: str, *, log_dir: Path, live: Collection[int]
) -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_ring.log_path(log_dir, session_id, name)
    path.touch()
    logs = [log_ring.LogFile(log.name, log.stat().st_mtime) for log in log_dir.glob("*.log")]
    for victim in log_ring.evictions(logs, live):
        old = log_dir / victim
        old.unlink(missing_ok=True)
        old.with_suffix(".sh").unlink(missing_ok=True)
    return path


# meiva:note
# Writes the launch file and returns the command that sources it.
def write_launch_file(line: str, path: Path) -> str:
    path.write_text(background_plan.launch_file_text(line, path))
    return background_plan.source_command(path)


# meiva:note
# One validated background run: the command, the session's label and starting directory, whether the session outlives the command, its hard lifetime and an explicit notice policy.
@dataclass(frozen=True)
class BackgroundRun:
    cmd: str
    name: str
    cwd: Path
    keep: bool
    ttl: float
    notify: str | None


# meiva:note
# What the process contributes to a run: the agent it belongs to, the CLI that delivers the completion notice, where the logs live and the machine facts a new session starts from.
@dataclass(frozen=True)
class BackgroundSite:
    agent_id: int
    cli: Path
    log_dir: Path
    host: SessionHost


# meiva:note
# Runs the command in a fresh persistent session and returns its handle. A run that fails to start leaves nothing behind: the launch file is removed and the session is killed, whatever step failed.
def start(shell: ShellSessions, run: BackgroundRun, site: BackgroundSite) -> SessionHandle:
    session_id, _full = shell.create(run.name, cwd=run.cwd, ttl=run.ttl, host=site.host)
    launch_path: Path | None = None
    try:
        output_path = allocate_output_path(
            session_id, run.name, log_dir=site.log_dir, live=frozenset(shell.list())
        )
        notice = background_plan.Notice(
            agent_id=site.agent_id,
            label=f"Background command '{run.name}'",
            source=f"shell:{session_id}",
            cli=site.cli,
        )
        line = background_plan.notified_line(
            run.cmd, notice, output_path=output_path, keep=run.keep, notify=run.notify
        )
        launch_path = output_path.with_suffix(".sh")
        shell.send(session_id, write_launch_file(line, launch_path))
    except Exception:
        if launch_path is not None:
            with contextlib.suppress(OSError):
                launch_path.unlink(missing_ok=True)
        try:
            shell.kill(session_id)
        except Exception:
            logger.opt(exception=True).warning(
                "killing background session {} after its failed start also failed; "
                "it stays until its TTL expires",
                session_id,
            )
        raise
    return SessionHandle(session_id=session_id, output_path=str(output_path))

# meiva:note
# What one Claude Code launch does, as a value: the session it opens, the line it types there, the first message it delivers and the files it prepares. The launcher reads the process, builds the plan from these and runs it step by step.
# meiva:link meiva/shell/coding_tools/docs/coding-tools.meiva.okf.md
from __future__ import annotations

import shlex
from dataclasses import dataclass
from pathlib import Path

from meiva.shell.coding_tools.plans.claude_screen import login_marker
from meiva.shell.coding_tools.plans.launch_request import (
    LaunchRequest,
    contract_file,
    worker_bootstrap,
)
from meiva.shell.coding_tools.plans.pacing import CLAUDE_READY, Pacing


# meiva:note
# The launch line: checks for the executable and login, then execs Claude Code. `caller_assignment` is the optional shell-safe external-caller assignment (`launch_caller_assignment`), empty without an opt-in. `claude_session` pins the session id up front (`--session-id`) so the launch can print it; with `resume` the same id reopens that recorded session (`--resume`) instead. A signed-out CLI still renders a ready panel and only fails on its first turn, so the line asks it directly before exec (exit 1 when logged out).
def claude_command(
    workspace: Path,
    caller_assignment: str = "",
    *,
    failure_marker: Path | None = None,
    claude_session: str | None = None,
    resume: bool = False,
) -> str:
    session_flag = ""
    if claude_session is not None:
        option = "--resume" if resume else "--session-id"
        session_flag = f" {option} {shlex.quote(claude_session)}"
    mark_failure = (
        f"printf '%s\\n' 'claude executable not found' > {shlex.quote(failure_marker.as_posix())}; "
        if failure_marker is not None
        else ""
    )
    mark_logout = (
        f"printf '%s\\n' 'not logged in' > {shlex.quote(login_marker(failure_marker).as_posix())}; "
        if failure_marker is not None
        else ""
    )
    return (
        f"cd {shlex.quote(workspace.as_posix())} && "
        "unset ANTHROPIC_API_KEY && "
        "claude_bin=$(command -v claude); "
        'if [ -z "$claude_bin" ]; then claude_bin="$HOME/.local/bin/claude"; fi; '
        'if [ ! -x "$claude_bin" ]; then '
        f"{mark_failure}"
        "printf '%s\\n' 'error: claude executable not found in PATH or "
        "$HOME/.local/bin/claude' >&2; exit 127; fi; "
        'if ! "$claude_bin" auth status >/dev/null 2>&1; then '
        f"{mark_logout}"
        "printf '%s\\n' 'error: claude is not logged in; run claude auth login' >&2; exit 126; fi; "
        f"{caller_assignment}"
        f'exec "$claude_bin" --dangerously-skip-permissions{session_flag} || exit $?'
    )


# meiva:note
# The files a supervised launch prepares: the task and work files it creates when absent and the work file's first content. A prompt launch prepares none.
@dataclass(frozen=True)
class WorkFiles:
    tasks_file: Path
    work_file: Path


# meiva:note
# One launch, decided. `message` is the first message delivered once the UI is up: the inline prompt of a prompt launch (typed through the composer's paste path) or the worker bootstrap of a supervised one (typed as it is). `provenance` is the dispatch block written at the top of the task file and `stamp` names the backups a first-run preset takes. `ready` is the launch's tuning for the panel wait.
@dataclass(frozen=True)
class ClaudePlan:
    workspace: Path
    session_name: str
    ttl_seconds: float
    launch_line: str
    failure_marker: Path
    claude_session: str
    resumed: bool
    files: WorkFiles | None
    message: str
    via_paste: bool
    provenance: str
    stamp: str
    ready: Pacing = CLAUDE_READY


# meiva:note
# What the launcher generates or reads for one launch: the Claude session id it pins or reopens, the private marker file the launch line writes when the executable is missing, the timestamp that names first-run backups and the external-caller assignment of the line.
@dataclass(frozen=True)
class ClaudeLaunchFacts:
    claude_session: str
    failure_marker: Path
    stamp: str
    caller_assignment: str


# meiva:note
# The plan for one launch of an already checked request. The session is named after the workspace directory.
def plan_claude_launch(request: LaunchRequest, facts: ClaudeLaunchFacts) -> ClaudePlan:
    resumed = request.resume is not None
    files = (
        WorkFiles(request.tasks_file, request.work_file)
        if request.tasks_file is not None and request.work_file is not None
        else None
    )
    if request.prompt is not None:
        message = request.prompt
    elif files is not None:
        message = worker_bootstrap(
            contract_file(request.skill_dir),
            request.workspace,
            files.tasks_file,
            files.work_file,
            resumed=resumed,
            provenance=request.provenance,
        )
    else:
        raise ValueError("a supervised launch needs its task and work files")
    return ClaudePlan(
        workspace=request.workspace,
        session_name=f"claude-{request.workspace.name}",
        ttl_seconds=request.ttl_seconds,
        launch_line=claude_command(
            request.workspace,
            facts.caller_assignment,
            failure_marker=facts.failure_marker,
            claude_session=facts.claude_session,
            resume=resumed,
        ),
        failure_marker=facts.failure_marker,
        claude_session=facts.claude_session,
        resumed=resumed,
        files=files,
        message=message,
        via_paste=request.prompt is not None,
        provenance=request.provenance or "",
        stamp=facts.stamp,
    )

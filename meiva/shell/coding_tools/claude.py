"""Launch, inspect, or stop a Claude Code session in a persistent shell.

A supervised worker (the default) keeps the two-file collaboration: a task
file, a work file, and a session the launcher's owner follows with the skill's
``watch_work.py``. An interactive prompt launch (``prompt``) starts Claude Code
without task/work files and delivers the prompt as its first message; an Meiva
agent that wants Claude Code to impersonate it simply spawns a session and
sends it a message with the impersonation instructions.

The ``meiva-guide.external-agents`` skill's ``spawn_claude.py`` is the command-line
entry; it passes its own skill directory, whose ``references/`` holds the
collaboration contract.
"""

from __future__ import annotations

import tempfile
import uuid
from pathlib import Path

from base.agents.messages.external_caller import launch_caller_assignment
from base.sessions import coding_session_owner

from ._claude_checks import send_bootstrap, wait_for_ready
from ._common import (
    LaunchSite,
    capability_report,
    cluster_home,
    cluster_sessions,
    init_file,
    kill_session_after_failed_launch,
    launch_site,
    write_task_file_header,
)
from ._common import cancel as _cancel_generation
from ._common import resolve_dir as resolve_dir
from ._common import resolve_file as resolve_file
from ._common import status as _owner_status
from ._first_run import preset_claude_first_run, pretrust
from .plans.claude_launch import ClaudeLaunchFacts, ClaudePlan, plan_claude_launch
from .plans.launch_request import LaunchRequest, checked_prompt
from .plans.launch_request import session_uuid as session_uuid

DEFAULT_TTL_SECONDS = 24 * 3600


def status(workspace: Path, *, mode: str = "auto", force: bool = False) -> int:
    """Print the workspace's recorded generations."""
    return _owner_status(
        coding_session_owner.canonical_key(workspace, tool="claude", cluster=cluster_home()),
        capability=capability_report("claude", mode=mode, force=force),
    )


def cancel(workspace: Path, generation: str) -> int:
    """Stop and terminalize exactly this generation."""
    return _cancel_generation(
        coding_session_owner.canonical_key(workspace, tool="claude", cluster=cluster_home()),
        generation,
        sessions=cluster_sessions(),
    )


# meiva:note
# Runs a launch plan in the site's shell sessions: prepares the files, trusts the workspace, refuses a session name already taken, opens the session, types the launch line, waits for the Claude Code panel and delivers the first message. A launch that fails after the session exists reclaims it and keeps its own error.
def run_supervised_launch(site: LaunchSite, plan: ClaudePlan) -> int:
    if plan.files is not None:
        init_file(plan.files.tasks_file, "")
        init_file(plan.files.work_file, "STATUS: WORKING\n\n## Log\n\n")
        write_task_file_header(plan.files.tasks_file, plan.provenance)
    pretrust(site.home, plan.workspace, plan.stamp)

    shell = site.shell
    if plan.session_name in shell.list().values():
        print(
            f"session '{plan.session_name}' already exists. "
            "session id: see `meiva.shell.sessions.list()`"
        )
        return 1

    sid = site.open_session(plan.session_name, plan.ttl_seconds)
    try:
        shell.send(sid, plan.launch_line)
        print(f"+ persistent shell session: {sid} ({plan.session_name})")
        wait_for_ready(
            shell,
            sid,
            plan.ready,
            site.timers,
            failure_marker=plan.failure_marker,
            resumed=plan.resumed,
        )
        if plan.via_paste:
            send_bootstrap(shell, sid, plan.message)
        else:
            shell.send(sid, plan.message)
    except BaseException:
        kill_session_after_failed_launch(shell, sid)
        raise

    print(f"ready. name={plan.session_name}  workspace={plan.workspace}")
    print(f"session_id={sid}")
    if plan.files is not None:
        print(f"tasks_file={plan.files.tasks_file}")
        print(f"work_file={plan.files.work_file}")
    print(f"claude_session={plan.claude_session}")
    if plan.files is not None:
        print(
            "NOTE: for xhigh effort on >10 files, consider STALL_SECONDS=1200+ "
            "in watch_work.py to avoid false stall alerts."
        )
    return 0


def launch(
    workspace: Path,
    tasks_file: Path | None,
    work_file: Path | None,
    ttl_seconds: float,
    caller_instance: str | None = None,
    *,
    skill_dir: Path,
    resume: str | None = None,
    prompt: str | None = None,
    provenance: str | None = None,
    preset_first_run: bool = True,
) -> int:
    """Launch a Claude Code session in a persistent shell.

    When ``prompt`` is given, sends that initial prompt without creating
    task/work files. Otherwise launches a supervised worker with ``tasks_file``
    and ``work_file``.
    """
    request = LaunchRequest(
        workspace,
        tasks_file,
        work_file,
        ttl_seconds,
        caller_instance,
        checked_prompt(prompt, tasks_file, work_file),
        skill_dir,
        resume,
        provenance,
    )
    caller_assignment = launch_caller_assignment("claude_code", caller_instance)
    site = launch_site()
    stamp = site.now.strftime("%Y%m%d-%H%M%S")
    if preset_first_run:
        preset_claude_first_run(site.home, stamp)
    with tempfile.TemporaryDirectory(prefix="meiva-claude-launch-") as marker_dir:
        facts = ClaudeLaunchFacts(
            claude_session=resume or str(uuid.uuid4()),
            failure_marker=Path(marker_dir) / "missing-claude",
            stamp=stamp,
            caller_assignment=caller_assignment,
        )
        plan = plan_claude_launch(request, facts)
        return run_supervised_launch(site, plan)
